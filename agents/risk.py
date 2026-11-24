"""Apply an explicit action policy after the evidence verdict."""
from agents.skeptic import words

READ_ONLY = {"collect diagnostics", "inspect service health", "open dashboard", "inspect queue", "inspect index"}
DESTRUCTIVE = {"delete", "drop", "erase", "truncate", "destroy", "purge"}


def assess(action, verdict, scars, version):
    normalized = " ".join(words(action))
    destructive = bool(set(normalized.split()) & DESTRUCTIVE)
    risky = destructive or any(w in normalized.split() for w in ("promote", "flush", "rebuild"))
    if destructive:
        verdict = "Blocked"
    elif risky and verdict == "Proceed":
        verdict = "Caution"
    score = None
    factors = []
    if scars:
        top = scars[0]
        score = 30 + 30 * (top["match"] == "equivalent") + 25 * (top["status"] == "confirmed") + 15 * (top["version"] == version)
        factors = [top["match"] + " action", top["status"] + " evidence", "current version" if top["version"] == version else "older version"]
    return {"level": "high" if risky else "low" if normalized in READ_ONLY else "medium",
            "verdict": verdict, "prohibited": destructive, "evidence_score": score,
            "factors": factors, "detail": "Destructive action is outside the sandbox action set." if destructive
            else "High-impact changes require supervision." if risky else "Bounded simulation action."}
