"""Who is asking, and whose data may they see.

Two independent answers, because they suit different situations:

* **Token scope** -- a token carries a list of subjects. Works everywhere, needs
  no Tailscale features, and is what a browser cookie resolves to.
* **Tailnet identity** -- the caller's tailnet IP is resolved through the local
  tailscaled to a Tailscale user, which is then mapped to subjects. No token to
  paste, which matters a great deal for a patient-facing page.

The identity is resolved from the *source address* via ``tailscale whois`` rather
than from a request header. Header-based identity (as injected by ``tailscale
serve``) is only trustworthy behind that proxy: this server is also reachable
directly on the tailnet, where any device could simply set the header itself. The
source address cannot be forged the same way, because the packet had to arrive
over the WireGuard tunnel of the node that owns that address.

Access is deny-by-default: an identity that is not in the mapping gets nothing,
rather than everything.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger("ppg_pi_server.identity")

#: Wildcard entry meaning "every subject", for the operator's own account.
ALL_SUBJECTS = "*"

#: whois results are cached briefly. A tailnet address maps to the same user for
#: the life of the node, and spawning a process per poll would be wasteful when
#: the page refreshes every 60 s.
WHOIS_CACHE_TTL_S = 300.0

_whois_cache: dict[str, tuple[float, str | None]] = {}


@dataclass(frozen=True)
class Viewer:
    """Who is asking, and what they may see."""

    #: Display label: a token's phone_id, or a Tailscale login.
    name: str

    #: Subjects this viewer may see. Empty means nothing; ``["*"]`` means all.
    subjects: tuple[str, ...]

    #: How the viewer was identified, for the audit log and the UI.
    method: str

    @property
    def sees_all(self) -> bool:
        return ALL_SUBJECTS in self.subjects

    def allowed(self, subject_id: str) -> bool:
        return self.sees_all or subject_id in self.subjects


def subjects_from_token(meta: dict) -> tuple[str, ...]:
    """Subjects a token may view.

    A token with no ``subjects`` key sees everything. That keeps existing tokens
    working exactly as before -- they were issued when there was no scoping, and
    silently narrowing them would break a phone's ability to see its own uploads.
    """
    raw = meta.get("subjects")
    if raw is None:
        return (ALL_SUBJECTS,)
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(s) for s in raw if str(s).strip())


def whois(addr: str, *, timeout_s: float = 2.0) -> str | None:
    """Resolve a tailnet ``ip:port`` to a Tailscale login, or None.

    Shells out to the ``tailscale`` CLI rather than speaking to the LocalAPI
    socket directly: the CLI is the supported interface, and the socket's HTTP
    protocol is not. Failure returns None, so an unavailable or non-Tailscale
    caller is simply unidentified rather than an error.
    """
    now = time.monotonic()
    hit = _whois_cache.get(addr)
    if hit and now - hit[0] < WHOIS_CACHE_TTL_S:
        return hit[1]

    login: str | None = None
    exe = shutil.which("tailscale")
    if exe is None:
        logger.debug("whois: tailscale CLI not found")
    else:
        try:
            out = subprocess.run(  # noqa: S603 - fixed executable, no shell
                [exe, "whois", "--json", addr],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            if out.returncode == 0:
                data = json.loads(out.stdout)
                login = (data.get("UserProfile") or {}).get("LoginName")
                if not login:
                    # Older CLI shapes nest the profile differently; fall back to
                    # the display name so a tagged device is still identifiable.
                    login = (data.get("UserProfile") or {}).get("DisplayName")
            else:
                logger.debug("whois failed for %s: %s", addr, out.stderr.strip()[:120])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.debug("whois error for %s: %s", addr, exc)

    _whois_cache[addr] = (now, login)
    return login


def viewer_for_login(login: str | None, access: dict[str, list[str]]) -> Viewer | None:
    """Map a Tailscale login onto a viewer, or None when it has no grant.

    Deny by default: an unlisted login sees nothing at all. A tailnet is shared
    with whoever was invited to it, and inheriting full access to someone's
    medical data by virtue of being on the same network is not a defensible
    default.
    """
    if not login:
        return None
    subjects = access.get(login)
    if subjects is None:
        logger.info("tailnet identity %s has no subject grant", login)
        return None
    return Viewer(name=login, subjects=tuple(subjects), method="tailnet")


def clear_cache() -> None:
    """Test hook."""
    _whois_cache.clear()
