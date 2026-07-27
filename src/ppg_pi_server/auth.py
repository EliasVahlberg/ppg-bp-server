"""Bearer-token authentication for the ingest API.

Tokens are 32-byte secrets (64 hex chars) generated once on Pi setup and
stored in the per-installation tokens file. The phone presents one as
``Authorization: Bearer <token>`` on every request.

This is a thin layer **on top of** Tailscale's network-level auth: the
server is only reachable to devices in the tailnet, and the bearer token
ties uploaded data to a specific phone identity in case multiple phones
ever join the tailnet.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings, get_settings
from .identity import (
    Viewer,
    subjects_from_token,
    viewer_for_login,
    whois,
)

logger = logging.getLogger("ppg_pi_server.auth")


#: Full access: may upload data. The phones hold these.
SCOPE_WRITE = "write"

#: Read-only: may view the web UI and status API, may not ingest.
#:
#: The web UI keeps its token in a browser cookie, which is a weaker place to
#: hold a secret than app-private storage. Separating the scope means a stolen
#: cookie can read data but cannot inject fabricated readings into a clinical
#: dataset, which would be both harder to detect and harder to undo.
SCOPE_READ = "read"

#: Cookie holding the viewer token. HttpOnly, SameSite=Strict, and Secure only
#: when the request arrived over HTTPS (plain HTTP over Tailscale is the current
#: transport, and a Secure cookie would simply never be sent back).
SESSION_COOKIE = "ppgbp_session"


def generate_token() -> str:
    """Return a new 64-char hex token (256 bits of entropy)."""
    return secrets.token_hex(32)


def token_scope(meta: dict) -> str:
    """Scope of a token. Tokens predating scopes are write, as they were."""
    return str(meta.get("scope") or SCOPE_WRITE)


def add_token(
    settings: Settings,
    phone_id: str,
    *,
    token: str | None = None,
    scope: str = SCOPE_WRITE,
    subjects: list[str] | None = None,
) -> str:
    """Add a token to the allowlist. Returns the (existing or new) token."""
    if scope not in (SCOPE_WRITE, SCOPE_READ):
        raise ValueError(f"unknown scope: {scope}")
    tokens = settings.load_tokens()
    # If the phone already has a token, return it (idempotent).
    for existing_token, meta in tokens.items():
        if meta.get("phone_id") == phone_id:
            return existing_token

    new_token = token or generate_token()
    tokens[new_token] = {
        "phone_id": phone_id,
        "created_at": datetime.now(UTC).isoformat(),
        "scope": scope,
    }
    # Absent key means "all subjects", so an unscoped token behaves exactly as
    # tokens did before scoping existed.
    if subjects:
        tokens[new_token]["subjects"] = list(subjects)
    settings.save_tokens(tokens)
    return new_token


def revoke_token(settings: Settings, token: str) -> bool:
    tokens = settings.load_tokens()
    if token in tokens:
        del tokens[token]
        settings.save_tokens(tokens)
        return True
    return False


async def require_bearer(
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = ...,
) -> dict:
    """FastAPI dependency that validates the bearer token.

    Returns the token's metadata dict (e.g. ``{"phone_id": "...", ...}``)
    so route handlers can attribute uploads to a known phone.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("auth failed: missing or malformed Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    tokens = settings.load_tokens()
    meta = tokens.get(token)
    if meta is None:
        logger.warning("auth failed: invalid token (prefix=%s…)", token[:8])
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid bearer token",
        )
    if token_scope(meta) != SCOPE_WRITE:
        logger.warning(
            "auth failed: read-only token used on a write route (phone_id=%s)",
            meta.get("phone_id"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is read-only",
        )
    return {"token": token, **meta}


def resolve_token(settings: Settings, token: str | None) -> dict | None:
    """Look a token up in the allowlist, returning metadata or None."""
    if not token:
        return None
    meta = settings.load_tokens().get(token)
    return None if meta is None else {"token": token, **meta}


async def require_viewer(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = ...,
) -> Viewer:
    """Identify the caller and decide which subjects they may see.

    A token wins when present, because it is an explicit choice by whoever pasted
    it. Otherwise, if tailnet identity is enabled, the source address is resolved
    through tailscaled. Failing both, 401.
    """
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if token is None:
        token = request.cookies.get(SESSION_COOKIE)

    meta = resolve_token(settings, token)
    if meta is not None:
        return Viewer(
            name=str(meta.get("phone_id") or "token"),
            subjects=subjects_from_token(meta),
            method="token",
        )

    if settings.tailnet_identity and request.client is not None:
        addr = f"{request.client.host}:{request.client.port or 0}"
        viewer = viewer_for_login(whois(addr), settings.subject_access)
        if viewer is not None:
            logger.info("tailnet viewer %s (%s)", viewer.name, addr)
            return viewer

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign in required",
    )


