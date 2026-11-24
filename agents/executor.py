"""A deterministic target simulator. Actions never reach operating systems or services."""
from agents.skeptic import relation
from store import SCENARIOS


def execute(service, version, action, verdict, approval=None):
    if verdict == "Blocked" and not (approval and approval["mode"] == "override"):
        return {"state": "blocked", "ran": False, "detail": "The simulator skipped the action."}
    if verdict == "Caution" and not approval:
        return {"state": "awaiting review", "ran": False, "detail": "The simulator is waiting for an on-call decision."}
    scenario = next(s for s in SCENARIOS if s[0] == service)
    fails = scenario[1] == version and relation(action, scenario[2]) == "equivalent"
    before = {"version": version, "health": "degraded"}
    after = {"version": version, "health": "unhealthy" if fails else "healthy"}
    return {"snapshot_before": before, "health_check": after["health"], "rollback": fails,
            "snapshot_after": before if fails else after, "environment": "simulation",
            "state": "failed" if fails else "succeeded", "ran": True, "supervised": bool(approval),
            "detail": scenario[4] if fails else "The simulated health check passed."}

