"""An explicit, bounded live demonstration driven by the worker."""
import json
import time

import ingress
import signals
from store import SCENARIOS, event, now, transaction, uid


def status(q):
    row = q("SELECT payload FROM controls WHERE name='demo'").fetchone()
    return json.loads(row["payload"]) if row else {"running": False, "remaining": 0}


def control(enabled, count=10):
    if type(enabled) is not bool or type(count) is not int or not 1 <= count <= 40:
        raise ValueError("Use a boolean enabled and a count between 1 and 40.")
    with transaction(True) as q:
        value = {"running": enabled, "remaining": count if enabled else 0, "sent": 0, "next_at": time.time(), "run_id": uid()}
        q("INSERT INTO controls VALUES ('demo',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (json.dumps(value),))
        event(q, "demo", {"state": "started" if enabled else "stopped", "count": count})
    return value


def tick():
    with transaction(True) as q:
        value = status(q)
        if not value["running"] or time.time() < value["next_at"]:
            return False
        service, version, action, symptom, _ = SCENARIOS[value["sent"] % len(SCENARIOS)]
        key = value["run_id"] + "-" + str(value["sent"])
        metrics = {"latency_ms": 950, "replication_lag_s": 12, "error_rate": 0.08}
        signals.record_sample(q, {"id": key, "service": service, "metrics": metrics}, "synthetic demo")
        ingress.enqueue(q, "simulator", key, service, "Synthetic live demo. " + symptom)
        value.update(remaining=value["remaining"]-1, sent=value["sent"]+1, next_at=time.time()+15)
        value["running"] = value["remaining"] > 0
        q("UPDATE controls SET payload=? WHERE name='demo'", (json.dumps(value),))
        return True
