"""ScanFit API: receives frames from the phone, queues jobs, serves results.

Served over HTTPS (the PC's Tailscale certificate) on the PC's Tailscale
address only (see scripts/run-server). Every route except /api/health needs
the pairing token that run-server puts in the phone's pairing link. CORS
admits only the web app's origin; state-changing requests from any other
browser origin are refused.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, jobs
from .jpeg import BadJpeg, strip_metadata

VERSION = __version__
log = logging.getLogger("scanfit")


def _origins() -> list[str]:
    out = []
    for o in os.environ.get("SCANFIT_WEB_ORIGIN", "").split(","):
        o = o.strip().rstrip("/")
        if not o:
            continue
        u = urlsplit(o)
        local = u.hostname in ("localhost", "127.0.0.1")
        if u.scheme not in ("https", "http") or (u.scheme == "http" and not local) or not u.hostname \
                or u.path or u.query or "*" in o:
            raise RuntimeError(f"SCANFIT_WEB_ORIGIN: {o!r} is not an https origin like https://scanfit-app.pages.dev")
        out.append(o)
    return out


def _token() -> bytes:
    """The pairing token: SCANFIT_TOKEN_FILE (written by run-server, mode 600)."""
    path = os.environ.get("SCANFIT_TOKEN_FILE")
    tok = Path(path).read_text().strip() if path else ""
    if len(tok) < 32:
        raise RuntimeError("no pairing token: start the server with scripts/run-server "
                           "(or set SCANFIT_TOKEN_FILE to a file holding a random token of 32+ characters)")
    return tok.encode()


ORIGINS = _origins()
TOKEN = _token()

store = jobs.Store()
worker = jobs.Worker(store)
uploads = asyncio.Semaphore(4)  # frames being checked and written at once


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not worker.is_alive():
        worker.start()
    yield


app = FastAPI(title="ScanFit", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

SECURITY_HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"content-security-policy", b"default-src 'none'"),
                    (b"cross-origin-resource-policy", b"same-origin")]
OPEN_PATHS = {"/api/health"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _send_json(status: int, detail: str):
    return JSONResponse({"detail": detail}, status_code=status)


class Guard:
    """Pure ASGI middleware, inside CORS (so refusals still carry CORS headers):
    - state-changing requests that carry a foreign browser Origin get 403;
    - every route except /api/health needs `Authorization: Bearer <token>` (401);
    - an unexpected error becomes a plain 500 (no traceback or path to the client)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        origin = headers.get("origin", "").rstrip("/")
        if scope["method"] not in SAFE_METHODS and origin and origin not in ORIGINS:
            return await _send_json(403, "origin not allowed")(scope, receive, send)
        if scope["path"] not in OPEN_PATHS and scope["method"] != "OPTIONS":
            auth = headers.get("authorization", "")
            given = auth[7:].strip().encode() if auth[:7].lower() == "bearer " else b""
            if not hmac.compare_digest(given, TOKEN):
                return await _send_json(401, "This phone is not paired with this PC.")(scope, receive, send)
        started = False

        async def send_tracked(msg):
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, receive, send_tracked)
        except Exception as exc:  # noqa: BLE001
            log.error("unexpected error: %s", type(exc).__name__)  # no paths or request data in the log
            if not started:
                await _send_json(500, "Something went wrong on the PC. Try again.")(scope, receive, send)


class SecurityHeaders:
    """Outermost: every response, including errors and refusals, gets these."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                names = {k.lower() for k, _ in msg.get("headers", [])}
                msg["headers"] = [*msg.get("headers", []), *((k, v) for k, v in SECURITY_HEADERS if k not in names)]
            await send(msg)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(Guard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["content-type", "authorization"],
    # Chrome's Private Network Access preflight: a public page (Cloudflare
    # Pages) calling a private/tailnet address must be explicitly allowed.
    allow_private_network=True,
    max_age=600,
)
app.add_middleware(SecurityHeaders)


def _job(jid: str):
    try:
        jobs.job_dir(jid)
    except ValueError:
        raise HTTPException(404, "unknown job")
    row = store.get(jid)
    if row is None or row["status"] == "deleted":
        raise HTTPException(404, "unknown job")
    return row


@app.get("/api/health")
def health():
    return {"ok": worker.healthy(), "name": "scanfit", "version": VERSION}


@app.get("/api/session", status_code=204)
def session():
    """Lets the phone check that its pairing token is right (the guard did)."""


@app.post("/api/jobs", status_code=201)
def create_job():
    if store.queued_count() >= jobs.MAX_QUEUE or store.open_count() >= jobs.MAX_OPEN:
        raise HTTPException(429, "The PC is busy. Try again in a few minutes.")
    if jobs.free_bytes() < jobs.MIN_FREE_BYTES:
        raise HTTPException(507, "The PC is running out of disk space.")
    return {"id": store.create(), "maxFrames": jobs.MAX_FRAMES, "maxFrameBytes": jobs.MAX_FRAME_BYTES}


def _save_frame(jid: str, side: int, index: int, body: bytes) -> None:
    try:
        data, w, h = strip_metadata(body)
    except BadJpeg as e:
        raise HTTPException(415, f"frames must be JPEG ({e})")
    if w * h > jobs.MAX_FRAME_PIXELS or max(w, h) > jobs.MAX_FRAME_SIDE:
        raise HTTPException(413, f"frame resolution too high ({w}x{h})")
    if jobs.job_bytes(jid) + len(data) > jobs.MAX_JOB_BYTES:
        raise HTTPException(413, "this scan is too large")
    if jobs.free_bytes() - len(data) < jobs.MIN_FREE_BYTES:
        raise HTTPException(507, "The PC is running out of disk space.")
    frames = jobs.job_dir(jid) / "frames"
    folder = frames / "side2" if side == 2 else frames
    if side == 2 and frames.is_dir():
        folder.mkdir(exist_ok=True)
    if not folder.is_dir():  # never re-create frames of a job that has ended
        raise HTTPException(409, "this scan is no longer accepting frames")
    out = folder / f"{index:04d}.jpg"
    tmp = folder / f".{index:04d}.{os.urandom(4).hex()}.part"
    tmp.write_bytes(data)
    tmp.replace(out)
    row = store.get(jid)
    if row is None or row["status"] != "uploading":  # the job started or ended meanwhile
        out.unlink(missing_ok=True)
        raise HTTPException(409, "this scan is no longer accepting frames")


@app.put("/api/jobs/{jid}/frames/{index}", status_code=204)
async def put_frame(jid: str, index: int, request: Request, side: int = 1):
    """side=2: the part filmed again after flipping it (optional second pass)."""
    row = _job(jid)
    if row["status"] != "uploading":
        raise HTTPException(409, "this scan is no longer accepting frames")
    if not 0 <= index < jobs.MAX_FRAMES:
        raise HTTPException(400, "too many frames")
    if side not in (1, 2):
        raise HTTPException(400, "side must be 1 or 2")
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "bad content-length")
    if declared > jobs.MAX_FRAME_BYTES:
        raise HTTPException(413, "frame too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > jobs.MAX_FRAME_BYTES:
            raise HTTPException(413, "frame too large")
    async with uploads:
        await run_in_threadpool(_save_frame, jid, side, index, bytes(body))


@app.post("/api/jobs/{jid}/start")
def start_job(jid: str):
    row = _job(jid)
    if row["status"] != "uploading":
        raise HTTPException(409, "already started")
    n = len(list((jobs.job_dir(jid) / "frames").glob("*.jpg")))
    n2 = len(list((jobs.job_dir(jid) / "frames" / "side2").glob("*.jpg")))
    if n < jobs.MIN_FRAMES:
        raise HTTPException(400, f"need at least {jobs.MIN_FRAMES} frames, got {n}")
    if 0 < n2 < jobs.MIN_FRAMES:
        raise HTTPException(400, f"the other side needs at least {jobs.MIN_FRAMES} frames, got {n2}")
    if store.queued_count() >= jobs.MAX_QUEUE:
        raise HTTPException(429, "The PC is busy. Try again in a few minutes.")
    if not store.transition(jid, "uploading", status="queued", frames=n + n2, started=time.time()):
        raise HTTPException(409, "already started")
    worker.wake.set()
    return {"status": "queued", "frames": n + n2, "sides": 2 if n2 else 1}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    row = _job(jid)
    out = {"id": jid, "status": row["status"], "frames": row["frames"]}
    if row["status"] == "queued":
        out.update(stage="queued", label="Waiting for the PC", progress=0.0,
                   position=store.queue_position(jid))
    elif row["status"] == "running":
        p = jobs.read_progress(jid)
        out.update(stage=p.get("stage", "mat"), label=p.get("label", "Starting"), progress=p.get("progress", 0.0),
                   step=p.get("step", ""), stats=p.get("stats", {}), preview=p.get("preview", 0),
                   frame=p.get("frame", 0), frame_label=p.get("frame_label", ""),
                   warnings=p.get("warnings", []), elapsed_s=p.get("elapsed_s", 0))
    elif row["status"] == "done":
        out["warnings"] = jobs.read_progress(jid).get("warnings", [])
    elif row["status"] == "failed":
        out.update(error=row["error"] or "processing failed", warnings=jobs.read_progress(jid).get("warnings", []))
    return out


RESULT_FILES = {"features.json": "application/json", "mesh.ply": "application/octet-stream",
                "check.jpg": "image/jpeg"}


@app.get("/api/jobs/{jid}/result/{name}")
def job_result(jid: str, name: str):
    row = _job(jid)
    if name not in RESULT_FILES:
        raise HTTPException(404, "unknown file")
    if row["status"] != "done":
        raise HTTPException(409, "not ready")
    path = jobs.job_dir(jid) / "result" / name
    if not path.is_file():
        raise HTTPException(410, "result expired")
    return FileResponse(path, media_type=RESULT_FILES[name])


@app.get("/api/jobs/{jid}/preview")
def job_preview(jid: str):
    """Live preview points while the job runs (small JSON, see worker preview.py)."""
    _job(jid)
    path = jobs.job_dir(jid) / "result" / "preview.json"
    if not path.is_file():
        raise HTTPException(404, "no preview yet")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{jid}/frame")
def job_frame(jid: str):
    """Thumbnail of the frame the PC is working on, the part tinted (see worker
    preview.py). Only while the job runs; it is deleted when the job ends."""
    row = _job(jid)
    path = jobs.job_dir(jid) / "result" / "frame.jpg"
    if row["status"] != "running" or not path.is_file():
        raise HTTPException(404, "no frame yet")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.delete("/api/jobs/{jid}", status_code=204)
def delete_job(jid: str):
    _job(jid)
    if not store.delete(jid):
        raise HTTPException(409, "the job is running; delete it when it ends")
