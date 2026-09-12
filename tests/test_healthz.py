"""Tests for GET /healthz -- the deep liveness probe added after the
2026-07-27 outage (see analysis/processing.py's process_canonical_store fix
in the polar-ppg-bp repo for the root cause this is meant to detect).

/health alone cannot catch this class of problem: the ingest server process
stayed up and answered /health with 200 the entire time the DuckDB store's
write lock was leaked by a separate process. /healthz actually opens the
store read-only, so a leaked lock shows up as a fast, clean 503 instead of
silence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from unittest import mock

import duckdb
import pytest
from fastapi.testclient import TestClient

_TMP = tempfile.TemporaryDirectory()
_DATA_DIR = Path(_TMP.name)
os.environ["PPG_PI_SERVER_DB_PATH"] = str(_DATA_DIR / "test.duckdb")
os.environ["PPG_PI_SERVER_UPLOAD_DIR"] = str(_DATA_DIR / "uploads")
os.environ["PPG_PI_SERVER_TOKENS_FILE"] = str(_DATA_DIR / "tokens.json")
(_DATA_DIR / "tokens.json").write_text(json.dumps({}))

from ppg_pi_server import main  # noqa: E402  (after env setup)
from ppg_pi_server.main import app  # noqa: E402  (after env setup)


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_healthz_ok_when_db_reachable(client: TestClient) -> None:
    # The app's own lifespan creates the DB on startup (via Ingestor's schema
    # init), so by the time the TestClient context is entered it should exist
    # and be readable.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["server"] == "ok"
    assert body["db"] == "ok"
    assert "elapsed_ms" in body


def test_healthz_no_auth_required(client: TestClient) -> None:
    # Deliberately unauthenticated -- same reasoning as GET /. A monitor must
    # be able to poll this without provisioning a token for it.
    resp = client.get("/healthz")  # no Authorization header
    assert resp.status_code in (200, 503)  # never 401/403


def test_healthz_reports_missing_db(client: TestClient, monkeypatch) -> None:
    from ppg_pi_server.config import Settings

    missing = Settings(
        db_path=Path("/nonexistent/does/not/exist.duckdb"),
        upload_dir=os.environ["PPG_PI_SERVER_UPLOAD_DIR"],
        tokens_file=os.environ["PPG_PI_SERVER_TOKENS_FILE"],
    )
    from ppg_pi_server import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = lambda: missing
    try:
        resp = client.get("/healthz")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
    assert resp.status_code == 503
    assert resp.json()["db"] == "missing"
    assert "recent_warnings" in resp.json()


def test_healthz_reports_no_ingest_yet_on_a_fresh_store(client: TestClient) -> None:
    """A brand-new store has zero rows in uploads/cuff_readings -- both
    last-activity fields must be None, not a crash or a fabricated value."""
    resp = client.get("/healthz")
    body = resp.json()
    assert body["db"] == "ok"
    assert body["last_ingest_at"] is None
    assert body["last_cuff_sync_at"] is None


def test_healthz_reports_last_ingest_time_from_the_uploads_table(client: TestClient) -> None:
    """The actual 'is it being reached and used' signal: a completed upload's
    timestamp must surface here, distinct from the DB merely being openable."""
    db_path = Path(os.environ["PPG_PI_SERVER_DB_PATH"])
    con = duckdb.connect(str(db_path))
    try:
        ts = 1_785_300_000.0  # arbitrary epoch seconds
        con.execute(
            "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, opened_at, "
            "completed_at, status) VALUES ('test-uuid', 'phone-01', ?, ?, 'complete')",
            [ts - 5, ts],
        )
    finally:
        con.close()

    body = client.get("/healthz").json()
    assert body["last_ingest_at"] is not None
    parsed = datetime.fromisoformat(body["last_ingest_at"])
    assert abs(parsed.timestamp() - ts) < 1.0


def test_healthz_reports_last_cuff_sync_time(client: TestClient) -> None:
    db_path = Path(os.environ["PPG_PI_SERVER_DB_PATH"])
    con = duckdb.connect(str(db_path))
    try:
        ts = 1_785_301_000.0
        con.execute(
            "INSERT INTO cuff_readings (reading_id, taken_at, sys, dia, pulse, "
            "ihb, mov, device, uploader_phone_id, uploaded_at) VALUES "
            "('r1', '2026-07-27T20:00:00', 120, 80, 65, false, false, 'omron', "
            "'phone-01', ?)",
            [ts],
        )
    finally:
        con.close()

    body = client.get("/healthz").json()
    assert body["last_cuff_sync_at"] is not None
    parsed = datetime.fromisoformat(body["last_cuff_sync_at"])
    assert abs(parsed.timestamp() - ts) < 1.0


def test_healthz_includes_recent_warnings(client: TestClient) -> None:
    import logging

    logger = logging.getLogger("ppg_pi_server.test_probe")
    marker = "TEST-MARKER: simulated analysis refresh failure"
    logger.warning(marker)

    body = client.get("/healthz").json()
    assert any(marker in line for line in body["recent_warnings"])


def test_healthz_reports_locked_fast_not_after_a_long_wait(client: TestClient) -> None:
    """The actual regression test for 2026-07-27's failure mode: hold a write
    lock on the same file from a *separate process* (a second connection in
    this same interpreter hits a different DuckDB error path -- see the
    same-process variant below), exactly like a leaked ppg-dashboard.service
    connection would, and confirm /healthz reports it as locked quickly --
    not by retrying for ~28s like /api/v1/status does, and not by hanging
    indefinitely."""
    db_path = Path(os.environ["PPG_PI_SERVER_DB_PATH"])
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            "import duckdb, time, sys; "
            "con = duckdb.connect(sys.argv[1]); "
            "print('locked', flush=True); "
            "time.sleep(30)",
            str(db_path),
        ],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "locked"  # subprocess has the write lock now

        t0 = time.perf_counter()
        resp = client.get("/healthz")
        elapsed = time.perf_counter() - t0

        assert resp.status_code == 503
        body = resp.json()
        assert body["server"] == "ok"
        assert body["db"] == "locked"
        # No retry loop: this must return in well under the ~28s an
        # /api/v1/status caller is willing to wait through.
        assert elapsed < 5.0
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_healthz_reports_same_process_connection_conflict_too(client: TestClient) -> None:
    """A second connection from within this same interpreter hits DuckDB's
    ConnectionException rather than IOException -- a different code path
    than the real cross-process outage, but /healthz should still surface it
    as "locked" rather than an opaque "error"."""
    db_path = Path(os.environ["PPG_PI_SERVER_DB_PATH"])
    holder = duckdb.connect(str(db_path))
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert resp.json()["db"] == "locked"
    finally:
        holder.close()


def test_healthz_recovers_once_the_lock_is_released(client: TestClient) -> None:
    db_path = Path(os.environ["PPG_PI_SERVER_DB_PATH"])
    holder = duckdb.connect(str(db_path))
    locked_resp = client.get("/healthz")
    assert locked_resp.json()["db"] == "locked"
    holder.close()

    ok_resp = client.get("/healthz")
    assert ok_resp.status_code == 200
    assert ok_resp.json()["db"] == "ok"


# --- data freshness -------------------------------------------------------
#
# Added after 2026-08-08..08-22: the patient's phone left the tailnet, uploads
# stopped for 14 days, and /healthz returned 200/db=ok throughout because the
# server itself was healthy. Reporting last_ingest_at was not enough -- the
# monitor gated on `db` alone and stayed green. These cover the judgement, not
# just the timestamp.


def _freshness(hours_ago: float | None, stale: float = 72.0, critical: float = 240.0) -> dict:
    """Classify a synthetic 'last ingest' that many hours in the past."""
    from datetime import timedelta, timezone as _tz

    from ppg_pi_server.main import _data_freshness

    now = datetime(2026, 8, 22, 8, 0, tzinfo=_tz.utc)
    iso = None if hours_ago is None else (now - timedelta(hours=hours_ago)).isoformat()
    return _data_freshness(iso, None, stale, critical, now=now)


def test_freshness_fresh_below_the_stale_threshold() -> None:
    assert _freshness(1.0)["data_status"] == "fresh"
    assert _freshness(71.9)["data_status"] == "fresh"


def test_freshness_stale_at_and_above_the_threshold() -> None:
    """Boundary is inclusive: exactly at the threshold counts as stale, so a
    threshold of 72h means 'three days with nothing', not 'more than'."""
    assert _freshness(72.0)["data_status"] == "stale"
    assert _freshness(100.0)["data_status"] == "stale"
    assert _freshness(239.9)["data_status"] == "stale"


def test_freshness_critical_at_and_above_the_threshold() -> None:
    assert _freshness(240.0)["data_status"] == "critical"
    # The real incident: 14 days offline.
    assert _freshness(14 * 24)["data_status"] == "critical"


def test_freshness_never_when_nothing_has_ever_arrived() -> None:
    """A fresh store must not read as 'critical' -- 'never ingested' is a
    setup state, not a fault, and conflating them would make every new
    deployment look broken."""
    out = _freshness(None)
    assert out["data_status"] == "never"
    assert out["hours_since_ingest"] is None


def test_freshness_reports_elapsed_hours() -> None:
    assert _freshness(48.0)["hours_since_ingest"] == pytest.approx(48.0, abs=0.2)


def test_freshness_verdict_ignores_cuff_sync_age() -> None:
    """Cuff uploads only happen on a manual Read Cuff (no periodic sync,
    android#11), so cuff staleness of days is normal and must not drive the
    overall verdict -- but it is still reported for a human to weigh."""
    from datetime import timedelta, timezone as _tz

    from ppg_pi_server.main import _data_freshness

    now = datetime(2026, 8, 22, 8, 0, tzinfo=_tz.utc)
    fresh_ingest = (now - timedelta(hours=1)).isoformat()
    ancient_cuff = (now - timedelta(days=30)).isoformat()
    out = _data_freshness(fresh_ingest, ancient_cuff, 72.0, 240.0, now=now)
    assert out["data_status"] == "fresh"
    assert out["hours_since_cuff_sync"] == pytest.approx(720.0, abs=1.0)


def test_freshness_tolerates_a_naive_timestamp() -> None:
    """Defensive: a store written by an older build could hold a timestamp
    without tzinfo. Treat it as UTC rather than raising inside a health probe."""
    from datetime import timezone as _tz

    from ppg_pi_server.main import _data_freshness

    now = datetime(2026, 8, 22, 8, 0, tzinfo=_tz.utc)
    out = _data_freshness("2026-08-22T06:00:00", None, 72.0, 240.0, now=now)
    assert out["hours_since_ingest"] == pytest.approx(2.0, abs=0.2)


def test_healthz_includes_freshness_fields_and_stays_200_when_stale() -> None:
    """A stale store is reachable, so it must not masquerade as an outage:
    'cannot read the store' and 'store is fine, nothing arriving' need
    different human responses."""
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "ok"
    for key in ("data_status", "hours_since_ingest", "hours_since_cuff_sync"):
        assert key in body
    assert body["data_status"] in {"fresh", "stale", "critical", "never"}


def test_healthz_counts_stuck_and_failed_conversions() -> None:
    """/complete now answers before converting, so a conversion that fails
    afterwards has no client left to report to. These counters are the only
    route to a human: an upload stuck in 'converting' means the bytes landed
    but the store never got them, which looks identical to an idle phone in
    every other field (last_ingest_at simply stops moving)."""
    db_path = Path(os.environ["PPG_PI_SERVER_DB_PATH"])
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, "
            "opened_at, status) VALUES ('stuck-uuid', 'phone-01', ?, 'converting')",
            [1_785_300_000.0],
        )
        con.execute(
            "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, "
            "opened_at, status) VALUES ('broken-uuid', 'phone-01', ?, 'error')",
            [1_785_300_000.0],
        )
    finally:
        con.close()

    with TestClient(app) as c:
        body = c.get("/healthz").json()
    assert body["conversions_pending"] >= 1
    assert body["conversions_failed"] >= 1

    con = duckdb.connect(str(db_path))
    try:
        con.execute("DELETE FROM uploads WHERE phone_session_uuid IN ('stuck-uuid', 'broken-uuid')")
    finally:
        con.close()


def test_healthz_reports_ok_while_its_own_conversion_holds_the_lock() -> None:
    """A conversion in progress is normal operation, not an outage.

    Conversion runs in a child process holding the store's write lock for
    70-270s, and DuckDB refuses a read-only open in that window. Reported
    naively that is a 503, i.e. every upload looks like a multi-minute outage
    on the widget. The cached timestamps are the current truth here rather than
    stale data, because last_ingest_at only advances once a conversion finishes.
    """
    with TestClient(app) as c:
        # Prime the cache with a real read.
        assert c.get("/healthz").status_code == 200

        ingestor = app.state.ingestor
        original = ingestor._converting_uuid
        ingestor._converting_uuid = "af1ff51f-dead-beef-0000-000000000000"
        try:
            with mock.patch.object(
                main.duckdb, "connect",
                side_effect=main.duckdb.IOException("Conflicting lock is held"),
            ):
                r = c.get("/healthz")
        finally:
            ingestor._converting_uuid = original

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["db"] == "ok"
    assert body["converting"] == "af1ff51f"
    assert body["conversions_pending"] == 1
    # Omitted rather than guessed at: it lives in the unreadable store.
    assert "conversions_failed" not in body


def test_healthz_still_503s_when_locked_without_a_conversion() -> None:
    """The 2026-07-27 case must keep reporting a fault.

    A lock held by something that is *not* our conversion -- the dashboard
    leaking one, say -- is the original reason this endpoint exists, and must
    not be swallowed by the conversion-aware branch.
    """
    with TestClient(app) as c:
        ingestor = app.state.ingestor
        original = ingestor._converting_uuid
        ingestor._converting_uuid = None
        try:
            with mock.patch.object(
                main.duckdb, "connect",
                side_effect=main.duckdb.IOException("Conflicting lock is held"),
            ):
                r = c.get("/healthz")
        finally:
            ingestor._converting_uuid = original

    assert r.status_code == 503
    assert r.json()["db"] == "locked"
