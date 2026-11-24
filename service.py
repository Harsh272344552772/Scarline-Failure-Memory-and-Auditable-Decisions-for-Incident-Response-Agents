"""The public mutation interface used by both server adapters."""
import hmac
import json
import os

import meko
from store import event, record, review, service_version, transaction, uid
from workflow import run


class FieldError(ValueError):
    pass


class Unavailable(Exception):
    pass


def authorize(header):
    configured = os.getenv("SCARLINE_USERS_JSON")
    try:
        users = json.loads(configured or "[]")
        if configured and (not isinstance(users,list) or not users or any(
                not isinstance(user,dict) or not isinstance(user.get("token"),str) or not user["token"]
                or not isinstance(user.get("name"),str) or not user["name"].strip()
                or user.get("role") not in {"viewer","operator","reviewer","admin"} for user in users)):
            raise ValueError()
    except (ValueError,TypeError):
        raise PermissionError("Workspace access configuration is invalid. Check the server settings.") from None
    if users:
        for user in users:
            if hmac.compare_digest(("Bearer " + user["token"]).encode(), header.encode()):
                return {"name": user["name"], "role": user["role"]}
        raise PermissionError("Enter an access token to open this workspace.")
    token = os.getenv("SCARLINE_OPERATOR_TOKEN")
    if token and not hmac.compare_digest(("Bearer " + token).encode(), header.encode()):
        raise PermissionError("Enter the operator token to make changes.")
    return None


def access_required():
    return bool(os.getenv("SCARLINE_OPERATOR_TOKEN") or os.getenv("SCARLINE_USERS_JSON"))


def enforce(identity, parts, body):
    if not identity:
        return body
    role = identity["role"]
    required = "operator"
    if parts[-1] in {"review", "approve", "override"}:
        required = "reviewer"
    elif parts[-1] in {"deploy", "runbook", "audit"}:
        required = "admin"
    allowed = {"operator": {"operator", "reviewer", "admin"}, "reviewer": {"reviewer", "admin"}, "admin": {"admin"}}
    if role not in allowed[required]:
        raise PermissionError("This action requires " + required + " access.")
    body = dict(body)
    for name in ("author", "reviewer"):
        if name in body or (name == "author" and parts == ["api", "scars"]) or (name == "reviewer" and required == "reviewer"):
            body[name] = identity["name"]
    return body


def fields(body, names):
    if not isinstance(body, dict):
        raise FieldError("Expected a JSON object.")
    selected = {}
    for name in names.split():
        value = body.get(name)
        limit = 1200 if name in {"symptom", "reason", "evidence", "note"} else 120
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
            raise FieldError(f"Check the {name} field.")
        selected[name] = value.strip()
    return selected


def change(parts, body, identity=None):
    if not isinstance(body, dict):
        raise FieldError("Expected a JSON object.")
    body = enforce(identity, parts, body)
    if parts == ["api", "demo"]:
        import simulator
        return 200, simulator.control(body.get("enabled"), body.get("count", 10))
    if parts == ["api", "telemetry"]:
        import signals
        import ingress
        with transaction(True) as q:
            before = signals.snapshot(q).get(body.get("service"), {}).get("state")
            result = signals.record_sample(q, body)
            if result.get("state") == "elevated" and before != "elevated":
                result["incident"] = ingress.enqueue(q, "telemetry", result["id"], body["service"], "; ".join(result["evidence"]))
        return 202, result
    if parts in (["api", "meko", "runbook"], ["api", "meko", "audit"]):
        import cloud_actions
        return 200, cloud_actions.perform(parts[-1])
    if parts == ["api", "decisions"]:
        return 201, run(**fields(body, "service action symptom"))
    if parts == ["api", "incidents"]:
        import ingress
        values = fields(body, "service symptom")
        with transaction(True) as q:
            service_version(q, values["service"])
            result = ingress.enqueue(q, "dashboard", uid(), **values)
        return 202, result
    if parts == ["api", "meko", "search"]:
        data = fields(body, "service action symptom")
        if not meko.enabled():
            raise Unavailable("Meko credentials are required.")
        result = meko.recall(uid(), **data)
        if result["status"] == "failed":
            raise Unavailable("Meko search was incomplete.")
        return 200, {"context": ["Scar " + key for key in result["scar_ids"]], **result}
    if parts == ["api", "scars"]:
        with transaction(True) as q:
            severity = body.get("severity", "medium")
            if severity not in {"low", "medium", "high", "critical"}:
                raise FieldError("Choose a valid severity.")
            key = record(q, **fields(body, "service action reason evidence author"), cloud=meko.enabled(), severity=severity)
        return 201, {"id": key}
    if len(parts) != 4 or parts[0] != "api":
        raise KeyError("Unknown endpoint")
    _, collection, key, verb = parts
    if (collection, verb) == ("incidents", "retry"):
        import ingress
        return 200, ingress.retry(key)
    if collection == "decisions":
        if verb == "replay":
            return 201, run(parent=key, origin="replay")
        if verb in {"approve", "override"}:
            approval = {**fields(body, "reviewer note"), "mode": verb}
            return 201, run(parent=key, approval=approval, origin="on-call review")
    with transaction(True) as q:
        if (collection, verb) == ("scars", "review"):
            values = fields(body, "reviewer status note")
            if values["status"] not in ("confirmed", "disputed"):
                raise ValueError("Choose a valid review status.")
            return 200, review(q, key, **values, cloud=meko.enabled())
        if (collection, verb) == ("services", "deploy"):
            version = fields(body, "version")["version"]
            before = service_version(q, key)
            if version == before:
                raise ValueError("Choose a different version.")
            q("UPDATE services SET version=? WHERE name=?", (version, key))
            return 200, event(q, "deployment", dict(service=key, previous=before, version=version))
    raise KeyError("Unknown endpoint")


def frame(revision):
    return f"id: {revision}\nevent: workspace\ndata: {json.dumps({'revision': revision})}\n\n"

