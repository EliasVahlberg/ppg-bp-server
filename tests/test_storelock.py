"""Tests for the cross-process store write lock.

The point of these is mutual exclusion between *processes*, so the contended
cases spawn a real child rather than using threads: a threading-based test
would pass even if the implementation used an in-process lock, which is exactly
the bug being prevented.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

from ppg_pi_server.storelock import lock_path_for, store_write_lock


def test_lock_path_sits_beside_the_database(tmp_path: Path) -> None:
    db = tmp_path / "sessions.duckdb"
    assert lock_path_for(db) == tmp_path / "sessions.duckdb.writelock"


def test_lock_path_is_stable_for_str_and_path(tmp_path: Path) -> None:
    """Both repos derive this path independently; it must not depend on the
    argument's type."""
    db = tmp_path / "sessions.duckdb"
    assert lock_path_for(db) == lock_path_for(str(db))


def test_acquires_when_uncontended(tmp_path: Path) -> None:
    db = tmp_path / "s.duckdb"
    with store_write_lock(db, timeout=5) as got:
        assert got is True


def test_records_the_holder_for_diagnosis(tmp_path: Path) -> None:
    """The DuckDB error names the blocking PID only after the fact. The lock
    file says who holds it while they still hold it."""
    db = tmp_path / "s.duckdb"
    with store_write_lock(db, timeout=5, label="convert abcd1234"):
        text = lock_path_for(db).read_text()
    assert "convert abcd1234" in text
    assert str(__import__("os").getpid()) in text


def _holder_script(db: Path, hold_s: float) -> str:
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).parent.parent / "src")!r})
        from ppg_pi_server.storelock import store_write_lock
        with store_write_lock({str(db)!r}, timeout=10, label="holder") as got:
            assert got, "child failed to acquire"
            print("ACQUIRED", flush=True)
            time.sleep({hold_s})
    """)


def test_a_second_process_is_excluded(tmp_path: Path) -> None:
    """The core guarantee: while another process holds the lock, we cannot."""
    db = tmp_path / "s.duckdb"
    child = subprocess.Popen(
        [sys.executable, "-c", _holder_script(db, 3.0)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ACQUIRED"

        # timeout=0 so this measures exclusion, not patience.
        with store_write_lock(db, timeout=0) as got:
            assert got is False, "two processes held the write lock at once"
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_waits_for_the_holder_then_proceeds(tmp_path: Path) -> None:
    """Contention should queue, not fail. This is the behaviour that replaces
    the DuckDB retry ladder reaching attempt 8 of 12 on 2026-10-21."""
    db = tmp_path / "s.duckdb"
    child = subprocess.Popen(
        [sys.executable, "-c", _holder_script(db, 1.5)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ACQUIRED"
        t0 = time.monotonic()
        with store_write_lock(db, timeout=30) as got:
            waited = time.monotonic() - t0
        assert got is True
        assert waited >= 1.0, f"returned too early ({waited:.2f}s); did it really wait?"
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_lock_is_released_when_the_holder_dies(tmp_path: Path) -> None:
    """flock is released by the kernel on process exit, so a crashed or killed
    writer cannot wedge the store. A hand-rolled lock file could not promise
    this, and oldpc has logged 156 unclean shutdowns."""
    db = tmp_path / "s.duckdb"
    child = subprocess.Popen(
        [sys.executable, "-c", _holder_script(db, 60.0)],
        stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ACQUIRED"
    child.kill()
    child.wait(timeout=10)

    with store_write_lock(db, timeout=10) as got:
        assert got is True, "lock survived the holder being killed"


def test_timeout_still_runs_the_body_when_not_required(tmp_path: Path) -> None:
    """Conversion must proceed even if it cannot get the lock: /complete has
    already answered 200 and the phone has dropped its retry state, so
    refusing to convert would lose the session outright."""
    db = tmp_path / "s.duckdb"
    child = subprocess.Popen(
        [sys.executable, "-c", _holder_script(db, 3.0)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ACQUIRED"
        ran = False
        with store_write_lock(db, timeout=0, required=False) as got:
            assert got is False
            ran = True  # the body is still expected to execute
        assert ran
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_the_dashboard_derives_the_same_lock_path() -> None:
    """Pin the cross-repository contract.

    polar-ppg-bp's /refresh computes this path independently, because the two
    services cannot import from each other. If the conventions drift, both
    sides keep working, each acquires a *different* lock, and mutual exclusion
    silently stops happening -- indistinguishable from having no lock at all,
    and only visible later as renewed DuckDB contention.

    Asserting on the literal pattern rather than importing the dashboard keeps
    this test honest even when polar-ppg-bp is not installed. If this fails
    after a deliberate change, update the dashboard's _store_lock_path too.
    """
    assert lock_path_for("/some/where/sessions.duckdb") == Path(
        "/some/where/sessions.duckdb.writelock"
    ), "ppg-bp-server and polar-ppg-bp must derive the same lock path"
