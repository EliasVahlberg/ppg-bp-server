"""Web UI: collection status, served by the ingest server itself.

Deliberately part of this service rather than a second one. The ingest server
already owns the token allowlist and is already bound to the Tailscale
interface, so putting the UI here means one auth model and one thing to keep
running. The separate Plotly dashboard on :8050 stays as it is for now; porting
its figures is a later job and not worth breaking working plots over.

Auth: any valid token may read, presented either as a bearer header (scripts) or
in a session cookie (browsers). Read-only tokens cannot ingest -- see
``auth.SCOPE_READ`` for why the scopes are split.

No build step, no framework, no CDN: one static HTML shell plus a small script
that polls the JSON API. A CDN dependency would also break the offline shell
once this is installed as a PWA.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import status as status_mod
from .auth import SESSION_COOKIE, require_viewer, resolve_token
from .config import Settings, get_settings

logger = logging.getLogger("ppg_pi_server.web")

STATIC_DIR = Path(__file__).parent / "static"

#: Cookie lifetime. Long, because the alternative is a patient locked out of her
#: own status page by an expiry she has no way to interpret.
COOKIE_MAX_AGE_S = 90 * 24 * 3600

router = APIRouter()


#: Retry budget when the store is locked. The analysis refresh
#: (``process_canonical_store``) holds a write lock for roughly 28 s, and DuckDB
#: refuses a read-only open from another process while it does, so a status
#: request landing during a refresh has to wait rather than fail.
LOCK_RETRY_ATTEMPTS = 8
LOCK_RETRY_MAX_WAIT_S = 5.0


def _read_only_connection(settings: Settings) -> duckdb.DuckDBPyConnection:
    """Open the store read-only, waiting out a concurrent writer.

    A fresh connection per request, so nothing is shared across threads. Read-only
    is deliberate: a status page must not be able to modify the store, and it also
    means several viewers never contend with each other -- only with an actual
    writer.
    """
    if not Path(settings.db_path).exists():
        raise HTTPException(503, f"Store not found: {settings.db_path}")
    last: Exception | None = None
    for attempt in range(LOCK_RETRY_ATTEMPTS):
        try:
            return duckdb.connect(str(settings.db_path), read_only=True)
        except duckdb.IOException as exc:
            last = exc
            if attempt == LOCK_RETRY_ATTEMPTS - 1:
                break
            wait = min(1.5 * (attempt + 1), LOCK_RETRY_MAX_WAIT_S)
            logger.info(
                "status: store locked (attempt %d/%d, retry in %.1fs)",
                attempt + 1,
                LOCK_RETRY_ATTEMPTS,
                wait,
            )
            time.sleep(wait)
    logger.warning("status: store still locked after %d attempts", LOCK_RETRY_ATTEMPTS)
    raise HTTPException(
        503,
        "Store is busy (an analysis refresh is probably running). Try again shortly.",
    ) from last


@router.get("/api/v1/status")
async def api_status(
    viewer: Annotated[dict, Depends(require_viewer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    con = _read_only_connection(settings)
    try:
        payload = status_mod.collect(con, timezone=settings.local_timezone)
    finally:
        con.close()
    payload["viewer"] = viewer.get("phone_id")
    return JSONResponse(content=payload)


@router.post("/app/login")
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Exchange a token for a session cookie.

    JSON rather than a form post so no multipart dependency is needed, and so the
    token never lands in a URL or the server access log.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Expected a JSON body") from None
    meta = resolve_token(settings, str(body.get("token", "")).strip())
    if meta is None:
        logger.warning("web login rejected")
        raise HTTPException(403, "Invalid token")
    resp = JSONResponse(content={"ok": True, "viewer": meta.get("phone_id")})
    resp.set_cookie(
        SESSION_COOKIE,
        meta["token"],
        max_age=COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return resp


@router.post("/app/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/app", response_class=HTMLResponse)
async def app_shell() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/app/{asset:path}")
async def app_asset(asset: str) -> FileResponse:
    """Serve the shell's own assets.

    Path traversal is prevented by resolving and then checking containment,
    rather than by pattern-matching the input.
    """
    target = (STATIC_DIR / asset).resolve()
    if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
        raise HTTPException(404, "Not found")
    return FileResponse(target)


@router.get("/sw.js")
async def service_worker() -> FileResponse:
    """Served from the root so the worker's scope covers the whole origin.

    Only registered by the page in a secure context, so over plain HTTP it is
    simply never used. That makes HTTPS (``tailscale serve``) the only thing
    standing between this and an installable offline-capable PWA.
    """
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@router.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


@router.get("/dashboard")
async def dashboard_redirect() -> RedirectResponse:
    """Point at the new UI from the old habit."""
    return RedirectResponse("/app")
