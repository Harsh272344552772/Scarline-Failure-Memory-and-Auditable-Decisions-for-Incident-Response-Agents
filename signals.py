"""Correlate three observed metrics within a five-minute window."""
import json
import math
from datetime import datetime, timedelta, timezone

from store import event, now, service_version, transaction, uid

LIMITS = {"latency_ms": 800, "replication_lag_s": 10, "error_rate": 0.05}


def evaluate(samples):
    if not samples:
        return {"state": "unknown", "score": None, "evidence": [], "detail": "Awaiting telemetry."}
    latest = samples[-1]
    current = json.loads(latest["metrics"])
    if datetime.fromisoformat(latest["created"]) < datetime.now(timezone.utc) - timedelta(minutes=5):
        return {"state": "stale", "score": None, "evidence": [], "detail": "Latest telemetry is over five minutes old."}
    previous = json.loads(samples[-2]["metrics"]) if len(samples) > 1 else {}
    evidence = [f"{key} = {value:g} (threshold {LIMITS[key]:g})"
                for key, value in current.items() if value >= LIMITS[key]]
    rising = [key for key, value in current.items() if key in previous and value > previous[key]]
    state = "elevated" if len(evidence) >= 2 else "watch" if evidence else "healthy"
    return {"state": state, "score": len(evidence), "evidence": evidence, "rising": rising,
            "detail": f"{len(evidence)} of three metric thresholds exceeded. Rule-based correlation.",
            "observed_at": latest["created"], "origin": latest["origin"]}


def record_sample(q, body, origin="telemetry"):
    if not isinstance(body, dict):
        raise ValueError("Expected a telemetry object.")
    service, key, metrics = body.get("service"), body.get("id"), body.get("metrics")
    if not isinstance(key, str) or not 1 <= len(key) <= 160:
        raise ValueError("Telemetry requires a unique sample ID.")
    if not isinstance(service, str) or not isinstance(metrics, dict) or set(metrics) != set(LIMITS):
        raise ValueError("Supply service and latency_ms, replication_lag_s, error_rate metrics.")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in metrics.values()) or metrics["error_rate"] > 1:
        raise ValueError("Use finite, nonnegative metrics and an error_rate between 0 and 1.")
    service_version(q, service)
    found = q("SELECT * FROM signals WHERE id=?", (key,)).fetchone()
    if found:
        if found["service"] != service or json.loads(found["metrics"]) != metrics:
            raise ValueError("Sample ID was already used for different telemetry.")
        return {"id": key, "duplicate": True}
    q("INSERT INTO signals VALUES (?,?,?,?,?)", (key, service, now(), json.dumps(metrics), origin))
    samples = [dict(r) for r in q("SELECT * FROM signals WHERE service=? ORDER BY created DESC LIMIT 3", (service,))]
    result = evaluate(list(reversed(samples)))
    event(q, "signal", {"service": service, "sample_id": key, "assessment": result})
    return {"id": key, **result}


def snapshot(q):
    result = {}
    for row in q("SELECT name FROM services ORDER BY name"):
        service = row["name"]
        samples = [dict(r) for r in q("SELECT * FROM signals WHERE service=? ORDER BY created DESC LIMIT 3", (service,))]
        result[service] = evaluate(list(reversed(samples)))
    return result
