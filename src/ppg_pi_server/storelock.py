"""A cross-process mutex for writers to the canonical DuckDB store.

DuckDB permits exactly one writing process per database file. Two long-running
writers exist for the live store and they are in different processes:

* this server's conversion child process (70-280s for a long session), and
* ``process_canonical_store`` in the analysis dashboard, which recomputes the
  ``derived_*`` tables (~28s and growing) and runs inside ``serve_dashboard.py``.

Nothing coordinated them, so they simply raced. Observed on the live store:

    2026-10-21 22:37:37  DB lock contention (attempt 1/12, retry in 2.0s):
      Could not set lock on file ".../sessions.duckdb": Conflicting lock is
      held in /usr/bin/python3.12 (PID 330281) by user elias

That episode reached attempt 8 of 12 before succeeding, and the mirror case
shows up as ``/refresh`` returning HTTP 500 when the server holds the lock
instead. Neither side was wrong; there was no mutual exclusion.

The ingest path's 12-attempt retry ladder is a backstop, not a solution: it
only resolves the collision if the other writer happens to finish inside the
ladder's budget. As recordings get longer, conversions hold the store for
longer, and the ladder gets closer to exhausting. If it ever does exhaust, the
conversion fails *after* /complete has already returned 200 and the phone has
written its .synced marker -- so the session would be dropped silently. That is
the failure this module exists to prevent.

Why an advisory file lock rather than an in-process lock: the two writers are
separate processes in separate repositories, and ``/refresh`` can also be
invoked by hand (it was, on 2026-09-12, while debugging). Coordination that
only works when the server initiates the refresh would break exactly when
someone intervenes manually.

**The lock path is a cross-repository contract.** Both sides derive it from the
database path with ``lock_path_for``, so there is nothing to configure and no
way for the two to disagree, but a change here must be mirrored in
``polar-ppg-bp``'s ``/refresh`` handler. ``flock`` is advisory and released
automatically when the holder exits or is killed, so a crashed writer cannot
wedge the store the way a stale lock file would.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

#: Long enough to outlast the longest plausible competing writer (a 24h session
#: converts in ~280s; a full derived rebuild is minutes), short enough that a
#: genuinely wedged holder is not waited on indefinitely.
DEFAULT_TIMEOUT_S = 900.0


def lock_path_for(db_path: Path | str) -> Path:
    """The lock file guarding ``db_path``.

    Kept beside the database rather than in /tmp or /run so it shares the
    database's lifetime and permissions: a store copied or moved to another
    machine does not inherit a lock from an unrelated host, and the lock cannot
    be lost to a tmpfs clean-up while a writer still holds it.
    """
    return Path(f"{db_path}.writelock")


@contextmanager
def store_write_lock(
    db_path: Path | str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    required: bool = False,
    label: str = "write",
) -> Iterator[bool]:
    """Hold the store's cross-process write lock.

    Yields True if the lock was acquired, False if it timed out.

    ``required=False`` (the default for conversion) means a timeout is logged
    and the body runs anyway, falling back to the DuckDB retry ladder. That is
    deliberate: by the time a conversion runs, /complete has already answered
    200 and the phone has discarded its retry state, so refusing to convert
    would lose the session outright. Proceeding is the lesser risk -- it is no
    worse than the behaviour before this lock existed.

    ``required=True`` (for the derived refresh) means the caller wants to be
    told instead, because a refresh is safely repeatable: skipping one and
    reporting "busy" costs nothing but a delay, whereas forcing it through would
    reintroduce the collision this module exists to remove.
    """
    lock_file = lock_path_for(db_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    t0 = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                waited = time.monotonic() - t0
                if waited >= timeout:
                    break
                if waited < 0.5:
                    # Record who we are waiting for, once, at the start. The
                    # DuckDB error names the blocking PID only after the fact;
                    # this says so before a human has to go looking.
                    logger.info(
                        "waiting for the store write lock (%s), held by %s",
                        label, _holder_description(lock_file),
                    )
                time.sleep(0.25)

        if acquired:
            os.truncate(fd, 0)
            os.write(fd, f"pid={os.getpid()} label={label} since={time.time():.0f}\n".encode())
            os.fsync(fd)
            waited = time.monotonic() - t0
            if waited > 1.0:
                logger.info("acquired the store write lock (%s) after %.1fs",
                            label, waited)
        else:
            level = logger.error if required else logger.warning
            level(
                "could not acquire the store write lock (%s) within %.0fs; "
                "holder: %s%s",
                label, timeout, _holder_description(lock_file),
                "" if required else " -- proceeding and relying on the DuckDB "
                                   "retry ladder",
            )
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _holder_description(lock_file: Path) -> str:
    """Best-effort identification of the current holder, for log messages."""
    try:
        text = lock_file.read_text().strip()
    except OSError:
        return "unknown"
    return text or "unknown"
