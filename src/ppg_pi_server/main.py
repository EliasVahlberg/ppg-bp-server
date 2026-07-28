"""FastAPI ingest server (ppg-pi-server).

Accepts raw ROP *session bundles* from the recorder, stages them, and
converts them into the canonical DuckDB store via the shared converter.

Routes:

- ``GET  /health``                              — liveness
- ``GET  /healthz``                              — liveness + DB reachability
  + last-ingest timestamps + recent warnings (fast, no-retry; distinguishes a
  locked/leaked DuckDB store from a down process, and a quiet-but-fine store
  from one that has actually stopped receiving data -- see docstring on the
  route)
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

import collections
import gzip
import json
import logging
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, AsyncIterator

import duckdb

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
    logging.getLogger().addHandler(recent_errors)


class _RecentErrorsHandler(logging.Handler):
    """Bounded in-memory ring buffer of WARNING+ log records.

    Exists for ``/healthz``: a monitor asking "is anything wrong" from outside
    the box has no access to ``journalctl`` (that's the whole point -- the
    2026-07-27 outage needed exactly that access, over SSH, to diagnose).
    Reading a log *file* from inside a request handler was considered and
    rejected -- this app has no configured log file, logging goes to
    stdout/journald, and coupling ``/healthz`` to "shell out to journalctl"
    would need extra permissions and tie the endpoint to systemd specifically.
    A ring buffer already living in the same process needs neither.

    Deliberately WARNING+ only, not INFO: this exists to answer "is there a
    problem", not to be a general log viewer. Deliberately capped at a small
    fixed size: this is diagnostic context for a monitor, not persistent
    storage -- history that matters belongs in the DB (uploads/cuff_readings
    timestamps) or the journal, not here.
    """

    def __init__(self, maxlen: int = 20) -> None:
        super().__init__(level=logging.WARNING)
        self._buf: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append(self.format(record))
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            pass

    def recent(self) -> list[str]:
        return list(self._buf)


recent_errors = _RecentErrorsHandler()
recent_errors.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
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
    # Skip noisy health checks at DEBUG when they succeed, so a periodic
    # monitor polling /healthz doesn't flood the log -- but any non-200 from
    # a health route (e.g. /healthz reporting the DB is locked) is exactly
    # the kind of thing worth seeing at INFO, not buried at DEBUG.
    is_health_route = request.url.path in ("/health", "/healthz")
    lvl = logging.DEBUG if is_health_route and response.status_code == 200 else logging.INFO
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


@app.get("/healthz")
async def healthz(settings: Annotated[Settings, Depends(get_settings)]) -> JSONResponse:
    """Deeper liveness probe: touch the DuckDB store and report what's actually
    been happening, not just whether a trivial query succeeds.

    ``/health`` only proves the process is alive -- it was still returning 200
    throughout the 2026-07-27 outage, because the process was fine; the store's
    write lock was leaked by a *different* process (the analysis dashboard).
    This endpoint exists to catch exactly that class of problem from outside,
    without needing adb/SSH access: a single-attempt, no-retry read-only open.

    Deliberately does not use the ``/api/v1/status`` retry-with-backoff logic
    (``_read_only_connection`` in ``web.py``) -- that logic exists to *tolerate*
    a normal ~28s refresh lock, which is the right behavior for a real viewer
    request. A monitoring probe wants the opposite: fail fast and say "locked"
    within a second, not silently wait up to ~28s before reporting anything.

    A bare "can I open the store" check is a shallow signal on its own -- it
    says the DB *can* be reached, not that anything real is happening through
    it. Two things are added to make a misdiagnosis less likely:

    - ``recent_warnings``: the last few WARNING+ log lines from this process
      (see ``_RecentErrorsHandler`` above), always included regardless of the
      DB check's outcome. A locked DB and the log line explaining *why* it got
      locked are more useful together than either alone.
    - ``last_ingest_at`` / ``last_cuff_sync_at``: the most recent timestamps
      actually written by real uploads (``uploads.completed_at`` and
      ``cuff_readings.uploaded_at``). This is the "is it actually being
      reached and used" signal -- a store that opens fine but hasn't received
      anything in days is a different problem than a locked store, and this
      endpoint could not previously tell the two apart.

    No auth: same reasoning as ``GET /`` above -- low-sensitivity (booleans, a
    millisecond count, timestamps, and log lines that are operational, not
    patient data), and this server already relies on network-layer isolation
    (Tailscale/LAN bind) rather than per-route auth for that class of endpoint.
    """
    warnings = recent_errors.recent()

    if not settings.db_path.exists():
        return JSONResponse(
            status_code=503,
            content={
                "server": "ok", "db": "missing", "detail": str(settings.db_path),
                "recent_warnings": warnings,
            },
        )

    t0 = time.perf_counter()
    try:
        con = duckdb.connect(str(settings.db_path), read_only=True)
        try:
            con.execute("SELECT 1").fetchone()
            last_ingest_at, last_cuff_sync_at = _last_activity(con)
        finally:
            con.close()
    except (duckdb.IOException, duckdb.ConnectionException) as exc:
        # IOException is the exact failure mode from 2026-07-27: a *separate
        # process* (typically ppg-dashboard.service) holds the write lock.
        # ConnectionException covers the same-process variant (DuckDB refuses
        # a second connection with different config in one interpreter) --
        # different code path, same practical meaning for a monitor: the
        # store cannot be read right now.
        return JSONResponse(
            status_code=503,
            content={
                "server": "ok",
                "db": "locked",
                "detail": str(exc).splitlines()[0],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "recent_warnings": warnings,
            },
        )
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe itself
        return JSONResponse(
            status_code=503,
            content={"server": "ok", "db": "error", "detail": str(exc), "recent_warnings": warnings},
        )
    return JSONResponse(
        content={
            "server": "ok",
            "db": "ok",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "last_ingest_at": last_ingest_at,
            "last_cuff_sync_at": last_cuff_sync_at,
            "recent_warnings": warnings,
        }
    )


def _last_activity(con: duckdb.DuckDBPyConnection) -> tuple[str | None, str | None]:
    """Most recent real-upload timestamps, as ISO-8601 UTC strings (or None).

    Both tables are server-owned (schema.py) and exist on any store this
    server has ever run against -- but a fresh/empty store legitimately has
    zero rows in either, hence the None handling rather than assuming a row
    exists.
    """
    def _max_epoch(table: str, column: str) -> str | None:
        try:
            row = con.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
        except duckdb.Error:
            return None
        if row is None or row[0] is None:
            return None
        return datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat()

    return _max_epoch("uploads", "completed_at"), _max_epoch("cuff_readings", "uploaded_at")


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
