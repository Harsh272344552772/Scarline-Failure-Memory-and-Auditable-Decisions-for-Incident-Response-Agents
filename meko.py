"""Server-side Meko tools. Local transactions never wait on network I/O."""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5


SCARLINE_PACK = "3985023e-ece5-4b81-9833-2132c3b7ce24"


def enabled():
    return bool(os.getenv("MEKO_API_KEY") and os.getenv("MEKO_DATAPACK_ID"))


@asynccontextmanager
async def connection():
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    headers = {"Authorization": "Bearer " + os.environ["MEKO_API_KEY"], "User-Agent": "meko-mcp-installer/1.0"}
    async with httpx.AsyncClient(headers=headers) as http:
        async with streamable_http_client("https://mcp.mekodata.ai/mcp", http_client=http) as (recv, send, _):
            async with ClientSession(recv, send) as session:
                await session.initialize()
                listed = await session.list_tools()
                session.scarline_tools = {tool.name: tool.inputSchema for tool in listed.tools}
                yield session


async def call(session, name, args, pack=None):
    if not name.startswith("project_"):
        args = {**args, "datapack_id": pack or os.environ["MEKO_DATAPACK_ID"]}
    schemas = getattr(session, "scarline_tools", None)
    if schemas is not None:
        schema = schemas.get(name)
        if schema is None or any(key not in args for key in schema.get("required", [])):
            raise ValueError("Meko tool unavailable or its required arguments changed.")
    log_call(name, args.get("datapack_id"), "attempted")
    try:
        result = await session.call_tool(name, args)
    except Exception:
        log_call(name, args.get("datapack_id"), "unverified")
        raise
    if result.isError:
        log_call(name, args.get("datapack_id"), "rejected")
        raise RuntimeError("Meko rejected " + name)
    body = result.structuredContent
    if body is None:
        blocks = [block.text for block in result.content if block.type == "text"]
        if len(blocks) != 1:
            raise ValueError("Unrecognized Meko response")
        body = json.loads(blocks[0])
    if not isinstance(body, dict) or body.get("error") or body.get("detail") or body.get("success") is False:
        raise ValueError("Unsuccessful Meko response")
    data = body.get("data")
    payload = data if isinstance(data, dict) else body
    if payload.get("error") or payload.get("success") is False:
        log_call(name, args.get("datapack_id"), "rejected")
        raise ValueError("Meko reported an unsuccessful operation.")
    log_call(name, args.get("datapack_id"), "acknowledged")
    return payload


def log_call(name, pack, state):
    from pathlib import Path
    from store import now, transaction, uid
    if not os.getenv("DATABASE_URL") and not Path(os.getenv("SCARLINE_DB", "data/scarline.sqlite3")).exists():
        return
    try:
        with transaction(True) as q:
            q("INSERT INTO meko_calls VALUES (?,?,?,?,?)", (uid(), now(), name, pack, state))
    except Exception:
        pass  # A diagnostic counter must not turn an acknowledged write into a retry.


OWNER = "scarline:incident"


async def conversation(session, trace_id, pack=None):
    session_id = str(uuid5(NAMESPACE_URL, pack + "/" + trace_id)) if pack and pack != os.environ["MEKO_DATAPACK_ID"] else trace_id
    data = await call(session, "conversation_create", {"agent_id": OWNER, "session_id": session_id,
                      "title": "Scarline incident " + trace_id[:8], "metadata": {"scarline_trace_id": trace_id}}, pack)
    obj = data.get("conversation", data)
    cid = obj.get("conversation_id") or obj.get("id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("Meko did not return a conversation ID")
    return cid


def references(result, service):
    # Only recall explicitly marked Scarline records from the memory bucket.
    # Conversation text and arbitrary instructions are never treated as scars.
    root = result.get("results", result)
    if not isinstance(root, dict):
        root = result
    bucket = root.get("memory")
    statuses = result.get("source_status") or result.get("source_statuses") or {}
    status = statuses.get("memory", "") if isinstance(statuses, dict) else ""
    if isinstance(bucket, dict):
        status = bucket.get("status", status)
        entries = bucket.get("results", bucket.get("items", bucket.get("memories", bucket.get("matches", bucket.get("hits")))))
    else:
        entries = bucket
    if isinstance(status, dict):
        status = status.get("status")
    ok = isinstance(entries, list) and str(status).lower() in {"", "ok", "success", "available", "complete", "completed", "searched"}
    ids = set()
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("app") == "scarline" and meta.get("service") == service:
            key = str(meta.get("scar_id", ""))
            if re.fullmatch(r"[0-9a-f]{32}", key):
                ids.add(key)
        body = str(item.get("memory") or item.get("text") or item.get("content") or "")
        prefix = re.match(r"^Scarline scar ([0-9a-f]{32}) for ([\w.-]+)\b", body)
        if prefix and prefix[2] == service:
            ids.add(prefix[1])
    return {"status": "ok" if ok else "partial", "scar_ids": sorted(ids)}


def search_items(result):
    items = result.get("results", result.get("chunks", result.get("matches")))
    if isinstance(items, dict):
        items = items.get("results", items.get("items", items.get("hits")))
    if not isinstance(items, list):
        raise ValueError("Search response did not contain an explicit result list.")
    return [item for item in items if isinstance(item, dict)]


async def _recall(trace_id, service, action, symptom):
    context = {"status": "failed", "scar_ids": [], "conversation_id": None, "sources": {}, "runbooks": []}
    try:
        async with asyncio.timeout(25), connection() as session:
            cid = await conversation(session, trace_id)
            context["conversation_id"] = cid
            query = f"Scarline failures and recovery for {service}. Action {action}. Incident {symptom}"
            args = {"conversation_id": cid, "agent_id": OWNER, "query": query, "limit": 5}
            kb_pack = os.getenv("MEKO_KNOWLEDGE_DATAPACK_ID") or os.environ["MEKO_DATAPACK_ID"]
            kb_cid = cid if kb_pack == os.environ["MEKO_DATAPACK_ID"] else await conversation(session, trace_id, kb_pack)
            responses = await asyncio.gather(
                call(session, "context_search", {**args, "force_source": "auto"}),
                call(session, "memory_search", args),
                call(session, "knowledgebase_search", {**args, "conversation_id": kb_cid}, kb_pack),
                return_exceptions=True)
            ids = set()
            for tool, result in zip(("context_search", "memory_search", "knowledgebase_search"), responses):
                context["sources"][tool] = "failed" if isinstance(result, Exception) else "ok"
            broad, memory, knowledge = responses
            if not isinstance(broad, Exception):
                found = references(broad, service)
                ids.update(found["scar_ids"])
                context["sources"]["context_search"] = found["status"]
            if not isinstance(memory, Exception):
                found = references({"memory": {"status": "ok", "results": search_items(memory)}}, service)
                ids.update(found["scar_ids"])
            if not isinstance(knowledge, Exception):
                context["runbooks"] = [{
                    "id": str(item.get("document_id") or item.get("id") or ""),
                    "source": str(item.get("document_name") or item.get("filename") or item.get("source") or "Meko knowledge"),
                    "text": str(item.get("text") or item.get("content") or item.get("chunk") or "")[:2000],
                } for item in search_items(knowledge)[:5]]
            context["scar_ids"] = sorted(ids)
            context["status"] = "ok" if all(v == "ok" for v in context["sources"].values()) else "partial"
    except Exception:
        pass
    return context


def recall(trace_id, service, action, symptom):
    if not enabled():
        return {"status": "local only", "scar_ids": [], "conversation_id": None}
    return asyncio.run(_recall(trace_id, service, action, symptom))


def enqueue(q, trace_id, item_id, kind, payload, conversation_id=None, pack=None):
    from store import now
    q("INSERT INTO cloud_jobs (id,trace_id,kind,payload,state,conversation_id,created,datapack_id) VALUES (?,?,?,?,?,?,?,?)",
      (item_id, trace_id, kind, json.dumps(payload), "queued", conversation_id, now(), pack or os.environ["MEKO_DATAPACK_ID"]))


async def deliver(job):
    from store import transaction
    payload = json.loads(job["payload"])
    cid = job["conversation_id"]
    target = job["datapack_id"]
    async with asyncio.timeout(18), connection() as session:
        if not cid:
            with transaction() as q:
                prev = q("SELECT conversation_id FROM cloud_jobs WHERE trace_id=? AND datapack_id=? AND conversation_id IS NOT NULL LIMIT 1",
                         (job["trace_id"], target)).fetchone()
            cid = prev["conversation_id"] if prev else await conversation(session, job["trace_id"], target)
            with transaction(True) as q:
                q("UPDATE cloud_jobs SET conversation_id=? WHERE id=?", (cid, job["id"]))
        args = {**payload, "conversation_id": cid}
        if job["kind"] != "track_token_usage":
            args["agent_id"] = OWNER
        await call(session, job["kind"], args, target)


def deliver_one():
    from store import transaction, now
    if not enabled():
        return False
    with transaction(True) as q:
        row = q("SELECT * FROM cloud_jobs WHERE state='queued' AND datapack_id IN (?,?,?,?) ORDER BY created,id LIMIT 1",
                (os.environ["MEKO_DATAPACK_ID"], os.getenv("MEKO_RAW_DATAPACK_ID", ""),
                 os.getenv("MEKO_KNOWLEDGE_DATAPACK_ID", ""), os.getenv("MEKO_AUDIT_DATAPACK_ID", ""))).fetchone()
        if not row:
            return False
        job = dict(row)
        q("UPDATE cloud_jobs SET state='sending' WHERE id=?", (job["id"],))
    try:
        asyncio.run(deliver(job))
        state = "acknowledged"
    except Exception:
        state = "unverified"
    with transaction(True) as q:
        q("UPDATE cloud_jobs SET state=?,updated=? WHERE id=?", (state, now(), job["id"]))
    return True

