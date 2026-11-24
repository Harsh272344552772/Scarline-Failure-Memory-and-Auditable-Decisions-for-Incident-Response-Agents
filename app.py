import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import ingress
import reporting
from service import FieldError, Unavailable, authorize, change, frame, access_required
from store import initialize, transaction


@asynccontextmanager
async def lifespan(app):
    initialize()
    stop = ingress.start_worker()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="Scarline", lifespan=lifespan)


@app.middleware("http")
async def protect_workspace(request, call_next):
    path = request.url.path
    if (path.startswith("/api/") or path == "/audit") and path not in {"/api/health", "/api/session"} and not path.startswith("/api/webhooks/"):
        try:
            request.state.identity = authorize(request.headers.get("authorization") or "Bearer " + request.cookies.get("scarline_access", ""))
        except PermissionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
    return await call_next(request)


@app.post("/api/session")
async def session(request: Request):
    import json
    data = json.loads(await body(request))
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not 1 <= len(token) <= 1024:
        raise FieldError("Enter an access token.")
    identity = authorize("Bearer " + token)
    response = JSONResponse({"role": (identity or {}).get("role", "operator")})
    response.set_cookie("scarline_access", token, httponly=True, secure=request.url.scheme == "https", samesite="strict", max_age=28800)
    return response


def error(status):
    async def handle(request, exc):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=status)
    return handle


for cls, code in ((KeyError, 404), (FieldError, 422), (ValueError, 400), (PermissionError, 403), (Unavailable, 503)):
    app.add_exception_handler(cls, error(code))


@app.get("/api/health")
def health():
    with transaction() as q:
        q("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/api/workspace")
def workspace():
    return {**reporting.workspace(), "operator_required": access_required()}


@app.get("/api/audit")
def audit():
    return JSONResponse(reporting.audit(), headers={"Content-Disposition": 'attachment; filename="scarline-audit.json"'})


@app.get("/audit", response_class=HTMLResponse)
def report():
    return reporting.report()


@app.get("/api/events")
async def feed(request: Request):
    async def stream():
        previous = request.headers.get("Last-Event-ID")
        while not await request.is_disconnected():
            current = await run_in_threadpool(reporting.revision)
            yield frame(current) if current != previous else ": heartbeat\n\n"
            previous = current
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def body(request):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise HTTPException(415, "Expected JSON.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 65536:
            raise HTTPException(413, "Request too large.")
        raw.extend(chunk)
    return bytes(raw)


@app.post("/api/webhooks/{source}", status_code=202)
async def webhook(source: str, request: Request):
    return await run_in_threadpool(ingress.receive, source, await body(request), request.headers)


@app.post("/api/decisions", status_code=201)
@app.post("/api/incidents", status_code=201)
@app.post("/api/scars", status_code=201)
@app.post("/api/meko/search")
@app.post("/api/meko/runbook")
@app.post("/api/meko/audit")
@app.post("/api/demo")
@app.post("/api/telemetry")
@app.post("/api/incidents/{key}/retry")
@app.post("/api/decisions/{key}/replay", status_code=201)
@app.post("/api/decisions/{key}/approve", status_code=201)
@app.post("/api/decisions/{key}/override", status_code=201)
@app.post("/api/scars/{key}/review")
@app.post("/api/services/{key}/deploy")
async def mutate(request: Request, key: str = ""):
    route = request.url.path.removeprefix("/api/")
    import json
    empty_replay = route.endswith("/replay") and request.headers.get("content-length") == "0"
    data = {} if empty_replay else json.loads(await body(request) or b"{}")
    status, result = await run_in_threadpool(change, ["api", *route.split("/")], data, getattr(request.state,"identity",None))
    return JSONResponse(result, status_code=status)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="dashboard")

