"""Configuration via environment variables.

All settings have sensible defaults for development. In production we
override paths via systemd's ``Environment=`` directives (see the unit
template under ``systemd/``).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server settings.

    Environment variables are prefixed with ``PPG_PI_SERVER_`` so that
    setting ``PPG_PI_SERVER_DB_PATH=/var/lib/ppg-pi-server/sessions.duckdb``
    overrides the default. ``.env`` files are also picked up.
    """

    model_config = SettingsConfigDict(
        env_prefix="PPG_PI_SERVER_",
        env_file=".env",
        extra="ignore",
    )

    # Storage
    db_path: Path = Field(
        default=Path("data/sessions.duckdb"),
        description="DuckDB file holding the canonical sessions store.",
    )
    upload_dir: Path = Field(
        default=Path("data/uploads"),
        description=(
            "Directory where staged raw ROP bundles are kept (one "
            "directory per session) as a belt-and-suspenders backup "
            "against DuckDB corruption."
        ),
    )
    keep_raw_uploads: bool = Field(
        default=True,
        description=(
            "If True, keep raw ROP bundles on disk after ingest. "
            "Protects against DB corruption."
        ),
    )

    # Auth
    tokens_file: Path = Field(
        default=Path("data/tokens.json"),
        description=(
            "JSON file containing the bearer-token allowlist. "
            'Format: {"token-hex-string": {"phone_id": "phone-01", '
            '"created_at": "2026-05-16T19:00:00"}}'
        ),
    )

    # Networking
    bind_host: str = Field(
        default="0.0.0.0",
        description=(
            "Host to bind. In production, set this to the Tailscale "
            "interface IP so the API is only reachable from the tailnet."
        ),
    )
    bind_port: int = Field(
        default=8000,
        description="HTTP port. We rely on Tailscale for transport encryption.",
    )

    # Limits
    max_upload_bytes: int = Field(
        default=200 * 1024 * 1024,
        description="Maximum upload size per request. ~200MB == 1h of calibration profile compressed.",
    )

    # Analysis hook
    analysis_refresh_url: str | None = Field(
        default=None,
        description=(
            "If set, the server fires a best-effort POST to this URL (the "
            "dashboard's /refresh) after a session completes or cuff readings "
            "are uploaded, so derived analysis tables get recomputed. Keeps the "
            "heavy analysis deps out of this lean ingest server."
        ),
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Root log level: DEBUG, INFO, WARNING, ERROR.",
    )

    def load_tokens(self) -> dict[str, dict]:
        """Load the bearer-token allowlist."""
        if not self.tokens_file.exists():
            return {}
        return json.loads(self.tokens_file.read_text())

    def save_tokens(self, tokens: dict[str, dict]) -> None:
        self.tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self.tokens_file.write_text(json.dumps(tokens, indent=2, sort_keys=True))


def get_settings() -> Settings:
    """Return a freshly-loaded Settings instance.

    Not memoized — the CLI may want to mutate the tokens file and have
    long-running servers see the change on next request.
    """
    return Settings()
