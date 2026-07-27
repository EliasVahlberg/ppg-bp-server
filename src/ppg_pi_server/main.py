"""FastAPI ingest server (ppg-pi-server).

Accepts raw ROP *session bundles* from the recorder, stages them, and
converts them into the canonical DuckDB store via the shared converter.

Routes:

- ``GET  /health``                              — liveness
- ``GET  /``                                    — landing page
- ``POST /api/v1/sessions``                     — open a session (audit row)
- ``PUT  /api/v1/upload/{uuid}/{filename}``     — stage one bundle file
- ``POST /api/v1/sessions/{uuid}/complete``     — convert the staged bundle
- ``GET  /api/v1/sessions``                     — list sessions (debug)
- ``GET  /api/v1/status``                       — collection status (read scope)
- ``GET  /app``                                 — status web UI (see web.py)

The phone uploads the bundle files (``manifest.json``, ``segments.jsonl``,
``notes.*``, ``*.rop``), optionally gzip-encoded, then calls ``complete``.

Note: ``GET /`` is intentionally unauthenticated (human-facing landing page)
and shows session UUID prefixes, device names, and sample counts. This is
low-sensitivity metadata, but the server relies on network-layer isolation
(bind to a Tailscale interface, or a trusted LAN) rather than app-level auth
for that one route. See ``config.Settings.bind_host``.
"""

from __future__ import annotations

import gzip
import json
import logging
import urllib.request
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path as PathParam,
    Request,
)
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .auth import require_bearer
from .config import Settings, get_settings
from .ingest import IngestError, Ingestor, valid_filename
from .web import router as web_router

logger = logging.getLogger("ppg_pi_server")


def _configure_logging() -> None:
    """Set up logging. Called once at import time."""
    # Use the env var PPG_PI_SERVER_LOG_LEVEL (default INFO).
    import os
    level = os.environ.get("PPG_PI_SERVER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s [%(funcName)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


_configure_logging()


def _trigger_analysis_refresh(url: str) -> None:
    """Best-effort POST to the dashboard's /refresh (runs in a background task)."""
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, method="POST", data=b""), timeout=120
        )
        logger.info("analysis refresh triggered: %s", url)
    except Exception as exc:  # noqa: BLE001 - never fail ingest on this
        logger.warning("analysis refresh failed (%s): %s", url, exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.ingestor = Ingestor(settings.db_path, settings.upload_dir)
    app.state.settings = settings
    logger.info("DB at %s, bundles at %s", settings.db_path, settings.upload_dir)
    try:
        yield
    finally:
        app.state.ingestor.close()


app = FastAPI(title="PPG-BP Pi backend", version="0.2.0", lifespan=lifespan)

# Web UI: status page and its JSON API. Same process as ingest, so there is one
# token allowlist and one service to keep running. See web.py.
app.include_router(web_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every inbound request with method, path, status, and duration."""
    import time as _time
    t0 = _time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (_time.perf_counter() - t0) * 1000
    # Skip noisy health checks at DEBUG, log everything else at INFO
    lvl = logging.DEBUG if request.url.path == "/health" else logging.INFO
    logger.log(lvl, "%s %s → %d (%.0fms)",
               request.method, request.url.path, response.status_code, elapsed_ms)
    return response


def get_ingestor(request: Request) -> Ingestor:
    return request.app.state.ingestor


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class OpenSessionRequest(BaseModel):
    phone_session_uuid: str
    device_name: str | None = None


class OpenSessionResponse(BaseModel):
    phone_session_uuid: str
    already_existed: bool


class UploadResponse(BaseModel):
    phone_session_uuid: str
    filename: str
    sha256_match: bool
    bytes: int


class CompleteResponse(BaseModel):
    phone_session_uuid: str
    status: str
    db_session_id: int
    samples_per_sensor: dict
    segments: int
    notes: int
    rop_files: int


class CuffReadingIn(BaseModel):
    id: str
    ts: str
    sys: int
    dia: int
    pulse: int
    ihb: bool = False
    mov: bool = False
    device: str | None = None

    # Clock provenance from ppg-bp-android#9. All optional: an older app build
    # sends none of them, and rejecting those uploads would strand readings on a
    # phone we cannot update remotely.
    phone_read_at: str | None = None
    clock_offset_s: float | None = None
    clock_offset_uncertainty_s: float | None = None
    clock_valid: bool | None = None
    clock_suspect: bool = False
    slot: int | None = None


class CuffUploadRequest(BaseModel):
    readings: list[CuffReadingIn]


class CuffUploadResponse(BaseModel):
    received: int
    inserted: int
    total: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.2.0"}


@app.get("/", response_class=HTMLResponse)
async def landing(ingestor: Annotated[Ingestor, Depends(get_ingestor)]) -> str:
    sessions = ingestor.list_sessions(limit=10)

    def _count(s: dict) -> object:
        try:
            st = json.loads(s["convert_stats_json"]) if s["convert_stats_json"] else {}
            return sum(st.get("samples_per_sensor", {}).values())
        except Exception:  # noqa: BLE001
            return "-"

    rows = "".join(
        f"<tr><td>{s['phone_session_uuid'][:8]}…</td>"
        f"<td>{s['device_name'] or '-'}</td>"
        f"<td>{s['status']}</td><td>{_count(s)}</td></tr>"
        for s in sessions
    )
    return f"""<!doctype html><html><head><title>PPG-BP Pi backend</title>
    <style>body{{font-family:system-ui,sans-serif;max-width:60em;margin:2em auto}}
    table{{border-collapse:collapse;width:100%}}
    th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left}}
    th{{background:#f0f0f0}}</style></head><body>
    <h1>PPG-BP Pi backend</h1>
    <p>Status: live. Ingest server for Polar Verity Sense ROP bundles.</p>
    <h2>Recent sessions</h2>
    <table><tr><th>UUID</th><th>Device</th><th>Status</th><th>Samples</th></tr>
    {rows or '<tr><td colspan="4"><em>none yet</em></td></tr>'}</table>
    </body></html>"""


@app.post("/api/v1/sessions", response_model=OpenSessionResponse)
async def open_session(
    body: OpenSessionRequest,
    auth: Annotated[dict, Depends(require_bearer)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
) -> OpenSessionResponse:
    existed = ingestor.open_session(
        phone_session_uuid=body.phone_session_uuid,
        uploader_phone_id=auth["phone_id"],
        device_name=body.device_name,
    )
    if existed:
        logger.info("session open (already existed): uuid=%s phone=%s",
                    body.phone_session_uuid[:8], auth["phone_id"])
    else:
        logger.info("session opened: uuid=%s device=%s phone=%s",
                    body.phone_session_uuid[:8], body.device_name, auth["phone_id"])
    return OpenSessionResponse(
        phone_session_uuid=body.phone_session_uuid, already_existed=existed
    )


@app.put(
    "/api/v1/upload/{phone_session_uuid}/{filename}",
    response_model=UploadResponse,
)
async def upload(
    request: Request,
    phone_session_uuid: Annotated[str, PathParam(min_length=8, max_length=64)],
    filename: Annotated[str, PathParam()],
    auth: Annotated[dict, Depends(require_bearer)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_sha256: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
) -> UploadResponse:
    """Stage one bundle file. SHA-256 is verified over the (decompressed) content."""
    if not valid_filename(filename):
        raise HTTPException(400, f"Illegal bundle filename: {filename!r}")
    if ingestor.get_session(phone_session_uuid) is None:
        raise HTTPException(404, "Session not opened. POST /api/v1/sessions first.")

    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty body")
    if (content_encoding or "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            logger.warning("upload gzip decode failed: uuid=%s file=%s err=%s",
                           phone_session_uuid[:8], filename, exc)
            raise HTTPException(400, f"Bad gzip body: {exc}") from exc
    if len(body) > settings.max_upload_bytes:
        logger.warning("upload too large: uuid=%s file=%s size=%d limit=%d",
                       phone_session_uuid[:8], filename, len(body), settings.max_upload_bytes)
        raise HTTPException(413, f"Upload exceeds {settings.max_upload_bytes} bytes")

    try:
        _sha, n = ingestor.stage_file(
            phone_session_uuid=phone_session_uuid,
            filename=filename,
            content=body,
            expected_sha256=x_sha256,
        )
    except IngestError as exc:
        logger.error("upload stage failed: uuid=%s file=%s err=%s",
                     phone_session_uuid[:8], filename, exc)
        raise HTTPException(400, str(exc)) from exc

    logger.debug("upload staged: uuid=%s file=%s bytes=%d sha=%s",
                 phone_session_uuid[:8], filename, n, _sha[:12])
    return UploadResponse(
        phone_session_uuid=phone_session_uuid,
        filename=filename,
        sha256_match=True,
        bytes=n,
    )


@app.post(
    "/api/v1/sessions/{phone_session_uuid}/complete",
    response_model=CompleteResponse,
)
async def complete(
    phone_session_uuid: str,
    background: BackgroundTasks,
    auth: Annotated[dict, Depends(require_bearer)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CompleteResponse:
    if ingestor.get_session(phone_session_uuid) is None:
        raise HTTPException(404, "Unknown session")
    logger.info("complete requested: uuid=%s phone=%s",
                phone_session_uuid[:8], auth["phone_id"])
    try:
        res = ingestor.complete(phone_session_uuid=phone_session_uuid)
    except IngestError as exc:
        logger.error("complete FAILED: uuid=%s err=%s", phone_session_uuid[:8], exc)
        raise HTTPException(400, str(exc)) from exc
    logger.info("complete OK: uuid=%s db_id=%d ppg=%d acc=%d gyro=%d segs=%d rops=%d",
                phone_session_uuid[:8], res.db_session_id,
                res.samples_per_sensor.get("ppg", 0),
                res.samples_per_sensor.get("acc", 0),
                res.samples_per_sensor.get("gyro", 0),
                res.segments, res.rop_files)
    # NOTE: analysis refresh is NOT triggered here — it's triggered by the cuff
    # endpoint (always the last thing synced) to avoid the 28s write lock from
    # process_canonical_store blocking subsequent cuff uploads in the same sync.
    return CompleteResponse(
        phone_session_uuid=res.phone_session_uuid,
        status="complete",
        db_session_id=res.db_session_id,
        samples_per_sensor=res.samples_per_sensor,
        segments=res.segments,
        notes=res.notes,
        rop_files=res.rop_files,
    )


@app.get("/api/v1/sessions")
async def list_sessions(
    auth: Annotated[dict, Depends(require_bearer)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
    limit: int = 100,
) -> JSONResponse:
    return JSONResponse(content={"sessions": ingestor.list_sessions(limit=limit)})


@app.post("/api/v1/cuff", response_model=CuffUploadResponse)
async def upload_cuff(
    body: CuffUploadRequest,
    background: BackgroundTasks,
    auth: Annotated[dict, Depends(require_bearer)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CuffUploadResponse:
    """Ingest standalone cuff readings (deduped by reading_id). Idempotent:
    the phone may re-upload its whole local store; only new rows are inserted."""
    received, inserted, total = ingestor.ingest_cuff_readings(
        readings=[r.model_dump() for r in body.readings],
        uploader_phone_id=auth["phone_id"],
    )
    logger.info("cuff upload: received=%d inserted=%d total=%d phone=%s",
                received, inserted, total, auth["phone_id"])
    if settings.analysis_refresh_url:
        background.add_task(_trigger_analysis_refresh, settings.analysis_refresh_url)
    return CuffUploadResponse(received=received, inserted=inserted, total=total)
