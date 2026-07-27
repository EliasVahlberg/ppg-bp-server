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
        default="127.0.0.1",
        description=(
            "Host to bind. Defaults to loopback-only so a fresh install "
            "never listens on the network before you've explicitly chosen "
            "an interface. In production, set this to your Tailscale "
            "interface IP (or 0.0.0.0 if you're handling access control "
            "with a firewall) so the API is reachable from the tailnet/LAN."
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

    tailnet_identity: bool = Field(
        default=False,
        description=(
            "If True, a caller with no token is identified by resolving its "
            "tailnet source address through the local tailscaled (tailscale "
            "whois) and looking the login up in subject_access. Lets a patient "
            "open the page with no token to paste. Off by default because it "
            "only makes sense when the server is reachable solely over "
            "Tailscale."
        ),
    )
    subject_access: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            'Tailscale login to subject grant, e.g. {"you@github": ["*"], '
            '"her@example.com": ["maggie-phone"]}. Deny by default: an unlisted '
            "login sees nothing. Set as JSON in "
            "PPG_PI_SERVER_SUBJECT_ACCESS."
        ),
    )

    local_timezone: str | None = Field(
        default=None,
        description=(
            "IANA zone used to interpret the cuff's local wall-clock timestamps "
            "when pairing them with recordings (e.g. Europe/Stockholm). Defaults "
            "to the server's own zone, which is correct when the phone and server "
            "share it."
        ),
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
