"""Explicit operator cloud operations with immutable local receipts."""
import asyncio
import base64
import hashlib
import json
import os

import knowledge
import meko
from store import event, transaction, uid


async def save_audit():
    from reporting import audit
    content = json.dumps(audit(), sort_keys=True, indent=2).encode()
    if len(content) > 900000:
        raise ValueError("Export is too large for inline verification. Download the audit JSON.")
    pack = os.getenv("MEKO_AUDIT_DATAPACK_ID") or os.environ["MEKO_DATAPACK_ID"]
    digest = hashlib.sha256(content).hexdigest()
    async with asyncio.timeout(40), meko.connection() as session:
        cid = await meko.conversation(session, "audit-" + digest[:24], pack)
        result = await meko.call(session, "artifact_put", {
            "filename": "scarline-audit.json", "content_base64": base64.b64encode(content).decode(),
            "content_type": "application/json", "agent_id": meko.OWNER, "conversation_id": cid
        }, pack)
        if result.get("content_hash") != digest:
            raise ValueError("Meko did not acknowledge the expected audit hash.")
        stored = await meko.call(session, "artifact_get", {
            "content_hash": digest, "agent_id": meko.OWNER, "conversation_id": cid
        }, pack)
        encoded = stored.get("content_base64")
        if not isinstance(encoded, str) or hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest() != digest:
            raise ValueError("Audit artifact bytes have not been verified.")
    with transaction(True) as q:
        event(q, "artifact", {"content_hash": digest, "datapack_id": pack, "bytes": len(content), "state": "verified"})
    return {"content_hash": digest, "bytes": len(content), "state": "verified"}


def perform(operation):
    if not meko.enabled():
        raise ValueError("Configure Meko credentials on the server.")
    task = knowledge.install() if operation == "runbook" else save_audit()
    try:
        return asyncio.run(task)
    except Exception:
        raise ValueError("Cloud operation is incomplete. Check Meko access, tool availability and indexing status.") from None
