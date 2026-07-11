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

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings

logger = logging.getLogger("ppg_pi_server.auth")


def generate_token() -> str:
    """Return a new 64-char hex token (256 bits of entropy)."""
    return secrets.token_hex(32)


def add_token(settings: Settings, phone_id: str, *, token: str | None = None) -> str:
    """Add a token to the allowlist. Returns the (existing or new) token."""
    tokens = settings.load_tokens()
    # If the phone already has a token, return it (idempotent).
    for existing_token, meta in tokens.items():
        if meta.get("phone_id") == phone_id:
            return existing_token

    new_token = token or generate_token()
    tokens[new_token] = {
        "phone_id": phone_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
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
    return {"token": token, **meta}
