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
from pydantic import BaseModel, Field

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
    # Date included deliberately: this buffer survives for the life of the
    # process (days), so a bare wall-clock time makes a warning from two days
    # ago look like it happened minutes ago -- the exact misdiagnosis this
    # endpoint exists to prevent.
    logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%m-%d %H:%M:%S"
    )
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
    # Optional because the deferred path answers before conversion has run, so
    # there is no db id or row count to report yet. status distinguishes the
    # two cases: "complete" (converted inline) vs "converting" (accepted, will
    # be converted in the background). The Android client ignores this body
    # entirely -- it treats any 2xx as success -- so the optionality exists for
    # human and test consumers, not for it.
    db_session_id: int | None = None
    samples_per_sensor: dict = Field(default_factory=dict)
    segments: int | None = None
    notes: int | None = None
    rop_files: int | None = None


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
async def healthz(
    settings: Annotated[Settings, Depends(get_settings)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
) -> JSONResponse:
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
    - ``data_status`` / ``hours_since_ingest``: the same signal, judged rather
      than merely reported. Reporting the timestamp was not enough in practice:
      on 2026-08-08 the phone left the tailnet and uploads stopped for 14 days
      while this endpoint returned 200/``db=ok`` the whole time, because the
      server really was fine. The monitor gated on ``db`` alone and stayed
      green, so nobody noticed until someone thought to read the timestamp.

    A stale store still returns **200 with** ``db="ok"``, not 503. "I cannot
    reach the store" and "the store is fine but nothing is arriving" need
    different responses from a human, and collapsing both into one red state
    would lose that -- the widget is expected to render ``data_status`` as its
    own indicator rather than folding it into the up/down verdict.

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
            backlog = _conversion_backlog(con)
            _LAST_ACTIVITY_CACHE["last_ingest_at"] = last_ingest_at
            _LAST_ACTIVITY_CACHE["last_cuff_sync_at"] = last_cuff_sync_at
        finally:
            con.close()
    except (duckdb.IOException, duckdb.ConnectionException) as exc:
        # IOException is the exact failure mode from 2026-07-27: a *separate
        # process* (typically ppg-dashboard.service) holds the write lock.
        # ConnectionException covers the same-process variant (DuckDB refuses
        # a second connection with different config in one interpreter) --
        # different code path, same practical meaning for a monitor: the
        # store cannot be read right now.
        #
        # ...unless it is *our own* conversion holding the lock, which is normal
        # operation rather than a fault. The cached timestamps are the current
        # truth in that window, not stale data: last_ingest_at only advances when
        # a conversion completes, so by definition it has not moved yet. Without
        # this branch every upload would show up as a 70-270s outage.
        if ingestor.converting_uuid and _LAST_ACTIVITY_CACHE:
            cached_ingest = _LAST_ACTIVITY_CACHE.get("last_ingest_at")
            cached_cuff = _LAST_ACTIVITY_CACHE.get("last_cuff_sync_at")
            return JSONResponse(
                content={
                    "server": "ok",
                    "db": "ok",
                    "converting": ingestor.converting_uuid[:8],
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "last_ingest_at": cached_ingest,
                    "last_cuff_sync_at": cached_cuff,
                    **_data_freshness(
                        cached_ingest,
                        cached_cuff,
                        settings.data_stale_hours,
                        settings.data_critical_hours,
                    ),
                    # conversions_pending is known from the in-flight conversion
                    # itself. conversions_failed is deliberately omitted rather
                    # than reported as 0: it lives in the store, which cannot be
                    # read right now, and a confident wrong zero is worse than a
                    # missing key.
                    "conversions_pending": 1,
                    "recent_warnings": warnings,
                },
            )
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
            **_data_freshness(
                last_ingest_at,
                last_cuff_sync_at,
                settings.data_stale_hours,
                settings.data_critical_hours,
            ),
            **backlog,
            "recent_warnings": warnings,
        }
    )


# Last successfully read activity timestamps.
#
# Kept so that a conversion in progress does not force the probe to say "cannot
# read the store". DuckDB refuses a read-only open while another process holds
# the write lock, and conversion now runs in a child process for 70-270s, so
# without this every upload would look like a multi-minute outage.
_LAST_ACTIVITY_CACHE: dict[str, str | None] = {}


def _conversion_backlog(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Counts of uploads whose conversion has not succeeded.

    Necessary because /complete now answers before converting: the client is
    told "done" and stops retrying, so a conversion that fails afterwards has
    no other route to a human. 'converting' that never clears means the bytes
    landed but the store never got them, which is invisible in every other
    field here -- last_ingest_at simply stays where it was, which looks the same
    as an idle phone.
    """
    out = {"conversions_pending": 0, "conversions_failed": 0}
    try:
        rows = con.execute(
            "SELECT status, count(*) FROM uploads "
            "WHERE status IN ('converting', 'error') GROUP BY status"
        ).fetchall()
    except duckdb.Error:
        return out
    for status, n in rows:
        key = "conversions_pending" if status == "converting" else "conversions_failed"
        out[key] = int(n)
    return out


def _hours_since(iso_ts: str | None, now: datetime) -> float | None:
    """Whole-ish hours between an ISO-8601 UTC timestamp and ``now`` (or None)."""
    if iso_ts is None:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return round((now - then).total_seconds() / 3600.0, 1)


def _data_freshness(
    last_ingest_at: str | None,
    last_cuff_sync_at: str | None,
    stale_hours: float,
    critical_hours: float,
    now: datetime | None = None,
) -> dict[str, object]:
    """Classify how long it has been since real data arrived.

    Split out as a pure function so the thresholds can be tested without
    standing up a store or faking a clock inside the endpoint.

    ``data_status`` is graded on ingest only, not on cuff sync. Cuff uploads
    are enqueued solely by a manual "Read Cuff" in the app -- there is no
    periodic sync (android#11) -- so cuff staleness routinely reaches days
    without anything being wrong, and folding it into the overall verdict
    would make the verdict meaningless. ``hours_since_cuff_sync`` is still
    reported so a human can judge it.

    Note this measures *arrival*, not recording: a phone that records happily
    while offline looks identical to one that records nothing. Distinguishing
    those needs a signal from the phone itself, which does not exist yet.
    """
    now = now or datetime.now(tz=timezone.utc)
    hours_ingest = _hours_since(last_ingest_at, now)
    if hours_ingest is None:
        status = "never"
    elif hours_ingest >= critical_hours:
        status = "critical"
    elif hours_ingest >= stale_hours:
        status = "stale"
    else:
        status = "fresh"
    return {
        "data_status": status,
        "hours_since_ingest": hours_ingest,
        "hours_since_cuff_sync": _hours_since(last_cuff_sync_at, now),
    }


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

    if settings.convert_async:
        # Answer before converting. See Settings.convert_async for why this is
        # safe and why it was needed: conversion of a long session outruns the
        # client's 60s readTimeout, and the client's response to a timeout is to
        # re-upload the entire bundle.
        try:
            ingestor.assert_ready_to_convert(phone_session_uuid)
        except IngestError as exc:
            logger.error("complete FAILED (pre-flight): uuid=%s err=%s",
                         phone_session_uuid[:8], exc)
            raise HTTPException(400, str(exc)) from exc
        ingestor.mark_converting(phone_session_uuid)
        background.add_task(_convert_in_background, ingestor, phone_session_uuid)
        logger.info("complete ACCEPTED: uuid=%s (converting in background)",
                    phone_session_uuid[:8])
        return CompleteResponse(
            phone_session_uuid=phone_session_uuid,
            status="converting",
        )

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


def _convert_in_background(ingestor: Ingestor, phone_session_uuid: str) -> None:
    """Run conversion after the response has been sent.

    Defined as a plain ``def`` on purpose: Starlette runs sync background tasks
    in a worker thread, so a 270-second CPU-bound conversion does not block the
    event loop (an ``async def`` here would stall every other request for the
    duration). ``Ingestor.complete`` already serialises on its own lock, so
    overlapping conversions queue rather than colliding on DuckDB's
    single-writer constraint.

    Swallows the exception because there is no longer a client to return it to:
    the response went out already. ``Ingestor.complete`` has set the upload's
    status to 'error' and logged with a traceback by this point, which puts it
    in /healthz's recent_warnings and in the conversions_failed count -- that is
    the whole reason those counters exist.
    """
    try:
        ingestor.complete(phone_session_uuid=phone_session_uuid)
    except Exception as exc:  # noqa: BLE001 - nowhere to propagate to
        logger.error("background conversion FAILED: uuid=%s err=%s",
                     phone_session_uuid[:8], exc)


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
