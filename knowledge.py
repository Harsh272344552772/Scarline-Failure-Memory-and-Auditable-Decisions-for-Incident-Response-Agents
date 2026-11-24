"""Upload the bundled runbook once per content hash and verify indexing."""
import asyncio
import base64
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

import meko
from store import transaction, uid

RUNBOOK = Path(__file__).parent / "runbooks" / "scarline-operations.md"
MARKER = "SCARLINE-RUNBOOK-20261124"


async def install():
    content = RUNBOOK.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    pack = os.getenv("MEKO_KNOWLEDGE_DATAPACK_ID") or os.environ["MEKO_DATAPACK_ID"]
    async with asyncio.timeout(180), meko.connection() as session:
        cid = await meko.conversation(session, "runbook-" + digest[:24], pack)
        with transaction() as q:
            previous = q("SELECT * FROM kb_files WHERE content_hash=? AND datapack_id=?", (digest, pack)).fetchone()
        key = previous["document_key"] if previous else None
        if not previous or previous["state"] in {"failed", "abandoned", "oversized"}:
            upload = await meko.call(session, "knowledgebase_upload_document", {
                "filename": RUNBOOK.name, "size": len(content), "content_hash": digest,
                "content_head_base64": base64.b64encode(content[:3072]).decode(), "conversation_id": cid
            }, pack)
            key = upload["key"]
            with transaction(True) as q:
                q("INSERT INTO kb_files VALUES (?,?,?,?,?) ON CONFLICT(content_hash,datapack_id) DO UPDATE SET document_key=excluded.document_key,state=excluded.state",
                  (digest, pack, RUNBOOK.name, key, "awaiting_upload"))
            if not upload.get("already_uploaded"):
                target = urlsplit(upload["url"])
                if target.scheme != "https" or not target.hostname or target.username or target.password:
                    raise ValueError("Invalid knowledge upload destination.")
                import httpx
                # The presigned form carries its own credentials.
                async with httpx.AsyncClient(timeout=30) as http:
                    response = await http.post(upload["url"], data=upload["fields"],
                                               files={"file": (RUNBOOK.name, content, "text/markdown")})
                    response.raise_for_status()
        for _ in range(60):
            result = await meko.call(session, "knowledgebase_document_status",
                                     {"document_key": key, "conversation_id": cid}, pack)
            state = result.get("state")
            with transaction(True) as q:
                q("UPDATE kb_files SET state=? WHERE content_hash=? AND datapack_id=?", (state or "unknown", digest, pack))
            if state == "completed":
                found = await meko.call(session, "knowledgebase_search", {
                    "conversation_id": cid, "agent_id": meko.OWNER,
                    "query": MARKER + " Scarline payments-api v2.14 worker restart connection pool runbook", "limit": 5
                }, pack)
                if not any(MARKER in str(item.get("text") or item.get("content") or item.get("chunk") or "") for item in meko.search_items(found)):
                    raise ValueError("Runbook indexed but its verification marker was not retrieved.")
                return {"state": "completed", "filename": RUNBOOK.name, "bytes": len(content), "content_hash": digest}
            if state in {"failed", "abandoned", "oversized"}:
                raise ValueError("Meko reported unsuccessful runbook indexing.")
            await asyncio.sleep(2)
    raise TimeoutError("Runbook indexing is still pending. Run verification again to resume.")
