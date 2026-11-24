"""Rank failure evidence for one service without letting fuzzy text authorize a block."""
import re

ALIASES = {"reboot": "restart", "recycle": "restart", "restarted": "restart",
           "workers": "worker", "replicas": "replica", "clear": "flush",
           "indices": "index", "indexes": "index"}
STOP = {"the", "a", "an", "please", "to", "of"}
TARGETS = {"worker", "replica", "index", "cache"}


def words(action):
    return tuple(ALIASES.get(w, w) for w in re.findall(r"[a-z0-9]+", action.lower()) if w not in STOP)


def relation(action, prior):
    left, right = words(action), words(prior)
    if left and left == right:
        return "equivalent"
    if set(left) & set(right) & TARGETS:
        return "related"
    return ""


def evaluate(service, version, action, ledger, recall):
    refs = set(recall.get("scar_ids", []))
    hits = []
    for scar in ledger:
        if scar["service"] != service:
            continue
        match = relation(action, scar["action"])
        via_meko = scar["id"] in refs
        if match or via_meko:
            hits.append({**scar, "match": match or "semantic", "via_meko": via_meko})

    def rank(s):
        return (s["match"] == "equivalent", s["version"] == version, s["status"] == "confirmed", s["created"])
    hits.sort(key=rank, reverse=True)
    if hits:
        top = hits[0]
        ref = f'Scar {top["id"][:8]} on v{top["version"]}'
        if top["match"] == "equivalent" and top["version"] == version and top["status"] == "confirmed":
            return "Blocked", f'{ref} records the same action. {top["reviewer"]} confirmed the failure. {top["reason"]}', hits
        if top["version"] != version:
            why = f'This service now runs v{version}. The earlier failure needs a supervised retest.'
        elif top["match"] != "equivalent":
            why = f'The earlier action was “{top["action"]}”. A reviewer must decide whether the same risk applies.'
        else:
            why = f'The earlier finding is {top["status"]}. An on-call reviewer must assess the risk.'
        return "Caution", f"{ref} may apply. {why}", hits
    if refs:
        return "Caution", "Meko recalled a Scarline record for this service. Its current review is unavailable in this ledger.", []
    if recall["status"] in {"failed", "partial"}:
        return "Caution", "The shared memory search was incomplete. A reviewer must check the proposal before it runs.", []
    return "Proceed", "The available failure ledger contains no matching scar for this service and action.", []

