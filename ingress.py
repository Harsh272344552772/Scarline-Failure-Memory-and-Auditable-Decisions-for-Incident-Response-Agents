"""Authenticate, normalize and durably queue monitoring alerts."""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import meko
from store import event, now, service_version, transaction, uid
from workflow import run

SOURCES = {"datadog", "pagerduty", "checkout-demo", "alertmanager", "grafana"}


def authenticate(source, raw, headers):
    if source not in SOURCES:
        raise KeyError("Unknown alert source")
    secret = os.getenv("SCARLINE_" + source.upper().replace("-", "_") + "_SECRET")
    if not secret:
        raise PermissionError("This alert source is disabled.")
    headers = {key.lower(): value for key, value in headers.items()}
    if source == "pagerduty":
        digest = "v1=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        valid = any(hmac.compare_digest(digest, s.strip()) for s in headers.get("x-pagerduty-signature", "").split(","))
    elif source == "grafana":
        signed = raw
        timestamp_header = os.getenv("SCARLINE_GRAFANA_TIMESTAMP_HEADER", "").lower()
        if timestamp_header:
            stamp = headers.get(timestamp_header, "")
            try:
                if abs(time.time() - int(stamp)) > 300:
                    raise ValueError()
            except ValueError:
                raise PermissionError("Grafana signature timestamp expired or missing.") from None
            signed = stamp.encode() + b":" + raw
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(digest, headers.get("x-grafana-alerting-signature", ""))
    elif source == "alertmanager":
        valid = hmac.compare_digest("Bearer " + secret, headers.get("authorization", ""))
    else:
        valid = hmac.compare_digest(secret.encode(), headers.get("x-scarline-token", "").encode())
    if not valid:
        raise PermissionError("Alert authentication failed.")


def normalize(source, data):
    if not isinstance(data, dict):
        raise ValueError("Expected an alert object.")
    if source in {"alertmanager", "grafana"}:
        alerts = data.get("alerts")
        if not isinstance(alerts, list) or not 1 <= len(alerts) <= 100:
            raise ValueError("Supply one to 100 grouped alerts.")
        normalized = []
        for alert in alerts:
            if not isinstance(alert, dict):
                raise ValueError("Each grouped alert must be an object.")
            labels, notes = alert.get("labels", {}), alert.get("annotations", {})
            if not isinstance(labels, dict) or not isinstance(notes, dict):
                raise ValueError("Expected alert labels and annotations.")
            start = alert.get("startsAt")
            state = alert.get("status")
            if state not in {"firing", "resolved"} or not isinstance(start, str) or not start:
                raise ValueError("Each alert needs status and startsAt.")
            fingerprint = alert.get("fingerprint") or json.dumps(labels, sort_keys=True)
            key = hashlib.sha256((str(fingerprint) + "/" + start).encode()).hexdigest()
            normalized.append((key, labels.get("service"), notes.get("description") or notes.get("summary") or labels.get("alertname"), state))
        return normalized
    if source == "pagerduty":
        ev = data.get("event") or {}
        if not isinstance(ev, dict) or not ev.get("event_type"):
            raise ValueError("Expected a PagerDuty v3 event.")
        if ev["event_type"] != "incident.triggered":
            return []
        detail = ev.get("data") or {}
        if not isinstance(detail, dict) or not isinstance(detail.get("service"), dict):
            raise ValueError("Expected a PagerDuty incident with a service.")
        return [(ev.get("id"), detail["service"].get("summary"), detail.get("title"), "firing")]
    return [(data.get("id"), data.get("service"), data.get("symptom"), "firing")]


def enqueue(q, source, key, service, symptom, status="firing"):
    external = source + "/" + key
    prev = q("SELECT trace_id,state FROM inbox WHERE id=?", (external,)).fetchone()
    if prev:
        if status == "resolved" and prev["state"] != "resolved":
            q("UPDATE inbox SET state='resolved' WHERE id=?", (external,))
            event(q, "alert_resolved", {"trace_id": prev["trace_id"], "service": service, "source": source})
            return {"trace_id": prev["trace_id"], "state": "resolved"}
        return dict(prev)
    trace = uid()
    state = "resolved" if status == "resolved" else "queued"
    q("INSERT INTO inbox (id,service,symptom,trace_id,created,state) VALUES (?,?,?,?,?,?)",
      (external, service, symptom, trace, now(), state))
    event(q, "alert", {"trace_id": trace, "service": service, "symptom": symptom, "source": source, "state": state})
    raw_pack = os.getenv("MEKO_RAW_DATAPACK_ID", "")
    if raw_pack and meko.enabled():
        if raw_pack == os.environ["MEKO_DATAPACK_ID"]:
            raise ValueError("Configure a separate raw datapack for alert intake.")
        meko.enqueue(q, trace, uid(), "conversation_add_message", {
            "input": f"Incoming {source} alert on {service}. {symptom}",
            "output": state,
            "metadata": {"app": "scarline", "source": source, "service": service, "scarline_trace_id": trace},
        }, pack=raw_pack)
    return {"trace_id": trace, "state": state}


def receive(source, raw, headers):
    authenticate(source, raw, headers)
    alerts = normalize(source, json.loads(raw))
    with transaction(True) as q:
        for key, service, symptom, status in alerts:
            if not all(isinstance(v, str) and v.strip() and len(v) <= limit
                       for v, limit in ((key, 160), (service, 120), (symptom, 1200))):
                raise ValueError("Expected an event ID, tracked service and symptom.")
            service_version(q, service)
        results = [enqueue(q, source, *alert) for alert in alerts]
    if source in {"alertmanager", "grafana"}:
        return {"alerts": results, "accepted": len(results)}
    return results[0] if results else {"state": "ignored"}


def consume_one():
    with transaction(True) as q:
        row = q("SELECT * FROM inbox WHERE state='queued' AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY created LIMIT 1",
                (now(),)).fetchone()
        if not row:
            return False
        job = dict(row)
        q("UPDATE inbox SET state='running',attempts=attempts+1 WHERE id=?", (job["id"],))
    try:
        source = job["id"].split("/",1)[0]
        origin = {"dashboard":"dashboard queue", "simulator":"live demo", "telemetry":"telemetry"}.get(source,"webhook")
        run(service=job["service"], symptom=job["symptom"], trace_id=job["trace_id"], origin=origin)
    except Exception as exc:
        attempts = job["attempts"] + 1
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=2 ** attempts)).isoformat()
        with transaction(True) as q:
            q("UPDATE inbox SET state=?,next_attempt=?,error=? WHERE id=? AND state='running'",
              ("failed" if attempts >= 3 else "queued", retry_at, type(exc).__name__, job["id"]))
            event(q, "agent", {"trace_id": job["trace_id"], "service": job["service"], "agent": "Workflow",
                               "state": "failed", "detail": "Attempt failed. " + ("Manual retry available." if attempts >= 3 else "Retry scheduled.")})
        logging.warning("Incident attempt failed (%s)", type(exc).__name__)
    return True


def retry(trace):
    with transaction(True) as q:
        row = q("SELECT state FROM inbox WHERE trace_id=?", (trace,)).fetchone()
        if not row or row["state"] != "failed":
            raise ValueError("Only a failed incident can be retried.")
        q("UPDATE inbox SET state='queued',attempts=0,next_attempt=NULL,error=NULL WHERE trace_id=?", (trace,))
        event(q, "retry", {"trace_id": trace})
    return {"trace_id": trace, "state": "queued"}


def start_worker():
    with transaction(True) as q:
        q("UPDATE inbox SET state='queued' WHERE state='running'")
        q("UPDATE cloud_jobs SET state='unverified' WHERE state='sending'")
    stop = threading.Event()

    def loop(cloud=False):
        import simulator
        while not stop.is_set():
            try:
                if cloud:
                    worked = meko.deliver_one()
                else:
                    simulator.tick()
                    worked = consume_one()
            except Exception as exc:
                logging.warning("Background worker failed (%s)", type(exc).__name__)
                worked = False
            stop.wait(0.02 if worked else 0.6)
    threading.Thread(target=loop, name="scarline-worker", daemon=True).start()
    threading.Thread(target=loop, args=(True,), name="scarline-cloud", daemon=True).start()
    return stop
