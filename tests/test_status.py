"""Tests for collection status and the web UI's auth boundary."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from ppg_pi_server import status as st
from ppg_pi_server.auth import SCOPE_READ, SCOPE_WRITE, add_token
from ppg_pi_server.config import Settings
from ppg_pi_server.main import app, get_ingestor
from ppg_pi_server.ingest import Ingestor
from ppg_pi_server.schema import init_audit_schema, init_cuff_schema

NOW = 1_785_000_000.0
DAY = 86400.0


# ---------------------------------------------------------------------------
# Pure assessment
# ---------------------------------------------------------------------------


def _payload(**subject_over) -> dict:
    subject = {
        "subject_id": "phone-01",
        "sessions": 5,
        "recorded_hours": 3.0,
        "last_session_at": NOW - 3600,
        "last_session_age_s": 3600.0,
        "cuff_readings": 40,
        "last_cuff_taken_at": "2026-07-26T19:58:08",
        "last_cuff_transfer_at": NOW - 3600,
        "last_cuff_transfer_age_s": 3600.0,
        "cuff_per_day": 2.0,
        "pairs": 25,
        "estimated_unsynced_cuff": 0.1,
    }
    subject.update(subject_over)
    return {
        "generated_at": NOW,
        "subjects": [subject],
        "clock": {"cuff_total": 40, "with_provenance": 40, "not_valid": 0, "suspect": 0},
        "quality": {"minutes": 100, "good_minutes": 95},
        "uploads": {"complete": 5},
    }


def _levels(warnings, message_part):
    return [w["level"] for w in warnings if message_part in w["message"]]


def test_healthy_collection_produces_no_warnings():
    assert st.assess(_payload()) == []


def test_stale_recording_is_an_error():
    w = st.assess(_payload(last_session_age_s=3 * DAY))
    assert _levels(w, "No recording for") == ["error"]


def test_no_recordings_at_all_warns():
    w = st.assess(_payload(last_session_age_s=None, last_session_at=None, sessions=0))
    assert _levels(w, "No recordings yet") == ["warn"]


def test_cuff_transfer_past_fourteen_days_is_an_error():
    """Matches the app's own red threshold, so the two never disagree."""
    w = st.assess(_payload(last_cuff_transfer_age_s=15 * DAY, estimated_unsynced_cuff=30.0))
    assert _levels(w, "Cuff not transferred") == ["error"]


def test_estimated_full_ring_is_an_error_about_unrecoverable_data():
    w = st.assess(_payload(estimated_unsynced_cuff=120.0))
    hit = [x for x in w if "buffer estimated full" in x["message"]]
    assert hit and hit[0]["level"] == "error"
    assert "unrecoverable" in hit[0]["detail"]


def test_filling_ring_warns_before_it_overflows():
    w = st.assess(_payload(estimated_unsynced_cuff=75.0))
    assert _levels(w, "Cuff buffer filling") == ["warn"]


def test_ring_below_threshold_is_silent():
    w = st.assess(_payload(estimated_unsynced_cuff=10.0))
    assert not [x for x in w if "buffer" in x["message"]]


def test_zero_pairs_warns_and_partial_pairs_informs():
    assert _levels(st.assess(_payload(pairs=0)), "No calibration pairs") == ["warn"]
    assert _levels(st.assess(_payload(pairs=6)), "6 of 20 calibration pairs") == ["info"]


def test_legacy_readings_without_provenance_are_informational_not_errors():
    """They cannot be repaired -- the cuff overwrote them -- so an error every day
    forever would only teach the reader to ignore errors."""
    p = _payload()
    p["clock"]["no_provenance"] = 4
    w = st.assess(p)
    hit = [x for x in w if "predate clock provenance" in x["message"]]
    assert hit and hit[0]["level"] == "info"
    assert not [x for x in w if x["level"] == "error"]


def test_invalid_clock_and_stalled_uploads_are_errors():
    p = _payload()
    p["clock"]["not_valid"] = 3
    p["uploads"] = {"complete": 4, "staged": 1}
    p["uploads_pending"] = {"count": 1, "in_flight": 0, "oldest_age_s": 7200.0}
    w = st.assess(p)
    assert _levels(w, "untrustworthy clock") == ["error"]
    assert _levels(w, "never completed") == ["error"]


def test_an_upload_still_in_flight_is_not_an_error():
    """The normal state for the first minutes after a recording ends. Reporting it
    as an error mid-session trains the reader to ignore the row that matters."""
    p = _payload()
    p["uploads"] = {"complete": 4, "staged": 1}
    p["uploads_pending"] = {"count": 1, "in_flight": 1, "oldest_age_s": 90.0}
    w = st.assess(p)
    assert _levels(w, "never completed") == []
    assert _levels(w, "in progress") == ["info"]


def test_an_upload_abandoned_long_ago_warns_but_is_not_an_error():
    """It must still be reported -- excluding it from `count` must not make it
    vanish -- but not as an error: the recording is not lost (the phone keeps
    local bundles) and no server-side action can clear it, so an error would be
    a permanent red mark nobody can act on from here."""
    p = _payload()
    p["uploads"] = {"complete": 4, "staged": 1}
    p["uploads_pending"] = {
        "count": 0, "in_flight": 0, "oldest_age_s": None,
        "abandoned": 3, "total_incomplete": 3,
    }
    w = st.assess(p)
    assert _levels(w, "no longer being retried") == ["warn"]
    assert not [x for x in w if x["level"] == "error"]


def test_abandoned_uploads_do_not_also_trip_the_stalled_error():
    """Regression guard for double-reporting: `stalled` is count - in_flight, and
    `count` now excludes abandoned rows, so an abandoned upload must produce
    exactly one message, at warn level, and no error."""
    p = _payload()
    p["uploads_pending"] = {
        "count": 0, "in_flight": 0, "oldest_age_s": None, "abandoned": 2,
    }
    w = st.assess(p)
    upload_msgs = [x for x in w if "never completed" in x["message"]]
    assert len(upload_msgs) == 1, f"expected one upload message, got {upload_msgs}"
    assert upload_msgs[0]["level"] == "warn"


def test_a_recently_stalled_upload_is_still_an_error():
    """The abandoned bucket must not swallow a genuinely stuck recent upload --
    that is the case that still needs someone to look."""
    p = _payload()
    p["uploads_pending"] = {
        "count": 1, "in_flight": 0, "oldest_age_s": 7200.0, "abandoned": 0,
    }
    assert _levels(st.assess(p), "never completed") == ["error"]


def test_poor_signal_quality_warns():
    p = _payload()
    p["quality"] = {"minutes": 100, "good_minutes": 40}
    assert _levels(st.assess(p), "usable") == ["warn"]


def test_errors_sort_before_warnings_and_info():
    p = _payload(last_session_age_s=5 * DAY, pairs=3)
    levels = [w["level"] for w in st.assess(p)]
    assert levels == sorted(levels, key=lambda x: {"error": 0, "warn": 1, "info": 2}[x])


@pytest.mark.parametrize(
    "per_day,age_s,expected",
    [
        (2.0, 10 * DAY, 20.0),
        (None, 10 * DAY, None),
        (2.0, None, None),
        (0.0, 10 * DAY, 0.0),
    ],
)
def test_unsynced_estimate(per_day, age_s, expected):
    assert st.estimated_unsynced_cuff(per_day, age_s) == expected


# ---------------------------------------------------------------------------
# Collection against a real store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "s.duckdb"
    con = duckdb.connect(str(db))
    init_audit_schema(con)
    init_cuff_schema(con)
    con.execute(
        """
        CREATE TABLE sessions (
            id INTEGER, session_uuid VARCHAR, start_time DOUBLE, end_time DOUBLE,
            device_name VARCHAR, device_address VARCHAR, settings VARCHAR,
            epoch_offset_ns BIGINT, rotation_period_minutes INTEGER
        )
        """
    )
    con.execute("CREATE TABLE notes (session_id INTEGER, ts DOUBLE, note VARCHAR)")
    con.close()
    return db


def _insert_recording(con, *, sid, uuid, start, end, uploader):
    con.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 'Polar Sense X', 'AA', '{}', 0, 15)",
        [sid, uuid, start, end],
    )
    con.execute(
        "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, device_name, "
        "opened_at, completed_at, status, files_json, convert_stats_json) "
        "VALUES (?, ?, 'Polar Sense X', ?, ?, 'complete', '[]', '{}')",
        [uuid, uploader, start, end],
    )


def _insert_cuff(
    con, *, reading_id, taken_at, uploader, offset=0.0, valid=True, suspect=False,
    uploaded_at=NOW - 3600,
):
    con.execute(
        "INSERT INTO cuff_readings (reading_id, taken_at, sys, dia, pulse, ihb, mov, "
        "device, uploader_phone_id, uploaded_at, phone_read_at, clock_offset_s, "
        "clock_offset_uncertainty_s, clock_valid, clock_suspect, slot) "
        "VALUES (?, ?, 120, 78, 70, FALSE, FALSE, NULL, ?, ?, "
        "'2026-07-27T12:00:00', ?, 1.0, ?, ?, 1)",
        [reading_id, taken_at, uploader, uploaded_at, offset, valid, suspect],
    )


def test_collect_on_empty_store_is_quiet_not_broken(store):
    con = duckdb.connect(str(store), read_only=True)
    try:
        out = st.collect(con, now=NOW)
    finally:
        con.close()
    assert out["subjects"] == []
    assert out["totals"]["cuff_readings"] == 0
    assert out["recent_cuff"] == []


def test_collect_groups_by_subject_and_never_mixes_them(store):
    """The whole point of the subject split: one person's data stays theirs."""
    con = duckdb.connect(str(store))
    _insert_recording(con, sid=1, uuid="u-a", start=NOW - 7200, end=NOW - 5400, uploader="phone-01")
    _insert_recording(con, sid=2, uuid="u-b", start=NOW - 3600, end=NOW - 1800, uploader="maggie-phone")
    _insert_cuff(con, reading_id="r1", taken_at="2026-07-20T10:00:00", uploader="phone-01")
    _insert_cuff(con, reading_id="r2", taken_at="2026-07-21T10:00:00", uploader="maggie-phone")
    _insert_cuff(con, reading_id="r3", taken_at="2026-07-25T10:00:00", uploader="maggie-phone")
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        out = st.collect(con, now=NOW)
    finally:
        con.close()

    by_id = {s["subject_id"]: s for s in out["subjects"]}
    assert set(by_id) == {"phone-01", "maggie-phone"}
    assert by_id["phone-01"]["sessions"] == 1
    assert by_id["phone-01"]["cuff_readings"] == 1
    assert by_id["maggie-phone"]["cuff_readings"] == 2


def test_pair_counting_requires_same_subject_and_a_trustworthy_clock(store):
    con = duckdb.connect(str(store))
    # A recording for phone-01 covering a known window.
    start, end = NOW - 3600, NOW - 1800
    _insert_recording(con, sid=1, uuid="u-a", start=start, end=end, uploader="phone-01")
    mid = con.execute("SELECT strftime(to_timestamp(?), '%Y-%m-%dT%H:%M:%S')", [start + 600]).fetchone()[0]

    _insert_cuff(con, reading_id="inside", taken_at=mid, uploader="phone-01")
    # Same instant, different subject: must not pair against someone else's PPG.
    _insert_cuff(con, reading_id="other-subject", taken_at=mid, uploader="maggie-phone")
    # Same instant, untrustworthy clock: the timestamp cannot be believed.
    _insert_cuff(con, reading_id="bad-clock", taken_at=mid, uploader="phone-01", valid=False)
    # Well outside the recording.
    _insert_cuff(con, reading_id="outside", taken_at="2026-01-01T10:00:00", uploader="phone-01")
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        out = st.collect(con, now=NOW)
    finally:
        con.close()
    by_id = {s["subject_id"]: s for s in out["subjects"]}
    assert by_id["phone-01"]["pairs"] == 1
    assert by_id["maggie-phone"]["pairs"] == 0


def test_cuff_wall_time_is_interpreted_in_the_local_zone(store):
    """Regression guard for a bug that made pairs silently vanish.

    ``taken_at`` is the cuff's local wall time; DuckDB's ``epoch()`` reads a naive
    timestamp as UTC. Interpreting it wrongly shifts every reading by the UTC
    offset, and the failure mode is zero pairs rather than an error, so it is only
    catchable by a test that would fail under the wrong interpretation.
    """
    con = duckdb.connect(str(store))
    tz = st.local_timezone(con)
    start, end = NOW - 1800, NOW  # a 30 min recording
    _insert_recording(con, sid=1, uuid="u-a", start=start, end=end, uploader="phone-01")
    # Wall-clock string for a moment inside the recording, in the local zone.
    wall = con.execute(
        "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')",
        [tz, start + 300],
    ).fetchone()[0]
    utc_offset = con.execute(
        "SELECT epoch(timezone(?, ?::TIMESTAMP)) - epoch(?::TIMESTAMP)", [tz, wall, wall]
    ).fetchone()[0]
    _insert_cuff(con, reading_id="inside", taken_at=wall, uploader="phone-01")
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        out = st.collect(con, now=NOW, timezone=tz)
    finally:
        con.close()
    assert out["timezone"] == tz
    assert out["subjects"][0]["pairs"] == 1
    if abs(utc_offset) > 0:
        # The zone is not UTC, so the naive-as-UTC reading really would miss.
        assert abs(utc_offset) >= 3600


def test_calibration_markers_are_parsed_from_notes(store):
    con = duckdb.connect(str(store))
    _insert_recording(con, sid=1, uuid="u-a", start=NOW - 3600, end=NOW - 1800, uploader="phone-01")
    con.execute(
        "INSERT INTO notes VALUES (1, ?, ?)",
        [NOW - 3500, '{"event":"calibration_start","name":"Run 1","tags":["supine"]}'],
    )
    con.execute("INSERT INTO notes VALUES (1, ?, ?)", [NOW - 3400, '{"note":"not a marker"}'])
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        out = st.collect(con, now=NOW)
    finally:
        con.close()
    assert len(out["markers"]) == 1
    assert out["markers"][0]["name"] == "Run 1"
    assert out["totals"]["notes"] == 2


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def client(store, tmp_path, monkeypatch):
    tokens = tmp_path / "tokens.json"
    settings = Settings(
        db_path=store, upload_dir=tmp_path / "up", tokens_file=tokens, bind_host="127.0.0.1"
    )
    monkeypatch.setattr("ppg_pi_server.config.Settings", lambda **kw: settings)
    write_token = add_token(settings, "phone-01", scope=SCOPE_WRITE)
    read_token = add_token(settings, "viewer", scope=SCOPE_READ)
    ingestor = Ingestor(store, tmp_path / "up")
    app.dependency_overrides[get_ingestor] = lambda: ingestor
    with TestClient(app) as c:
        yield c, write_token, read_token
    app.dependency_overrides.clear()


def test_status_requires_authentication(client):
    c, _, _ = client
    assert c.get("/api/v1/status").status_code == 401


def test_status_accepts_bearer_and_cookie(client):
    c, write_token, read_token = client
    assert c.get(
        "/api/v1/status", headers={"Authorization": f"Bearer {read_token}"}
    ).status_code == 200

    assert c.post("/app/login", json={"token": read_token}).status_code == 200
    r = c.get("/api/v1/status")  # cookie now set on the client
    assert r.status_code == 200
    assert r.json()["viewer"] == "viewer"


def test_login_rejects_an_unknown_token_and_sets_no_cookie(client):
    c, _, _ = client
    r = c.post("/app/login", json={"token": "0" * 64})
    assert r.status_code == 403
    assert "ppgbp_session" not in r.cookies
    assert c.get("/api/v1/status").status_code == 401


def test_read_only_token_cannot_ingest(client):
    """A cookie is a weaker secret store than app-private prefs, so the viewer
    scope must not be able to write fabricated readings into clinical data."""
    c, write_token, read_token = client
    payload = {"phone_id": "viewer", "readings": []}
    r = c.post(
        "/api/v1/cuff", json=payload, headers={"Authorization": f"Bearer {read_token}"}
    )
    assert r.status_code == 403
    ok = c.post(
        "/api/v1/cuff", json=payload, headers={"Authorization": f"Bearer {write_token}"}
    )
    assert ok.status_code < 400


def test_logout_clears_access(client):
    c, _, read_token = client
    c.post("/app/login", json={"token": read_token})
    assert c.get("/api/v1/status").status_code == 200
    c.post("/app/logout")
    assert c.get("/api/v1/status").status_code == 401


def test_app_shell_and_pwa_assets_are_served(client):
    c, _, _ = client
    # The shell itself is public: it contains no data, only the sign-in form.
    assert c.get("/app").status_code == 200
    assert "manifest" in c.get("/app").text
    assert c.get("/manifest.webmanifest").status_code == 200
    assert c.get("/sw.js").status_code == 200
    assert c.get("/app/app.js").status_code == 200


def test_asset_route_refuses_path_traversal(client):
    c, _, _ = client
    assert c.get("/app/../config.py").status_code in (404, 400)
    assert c.get("/app/%2e%2e/config.py").status_code in (404, 400)


# ---------------------------------------------------------------------------
# Why pairing failed
# ---------------------------------------------------------------------------
#
# These matter during a session rather than afterwards: the reason has to be on
# the page while the person and the equipment are still in the room, because that
# is the only moment when the run can be repeated.


def test_no_pairs_reports_the_largest_group_not_the_first_one_found():
    """Against the live store, a fixed ordering led with 4 readings lacking a clock
    offset while 100 had simply been taken outside any recording. True, but it
    pointed at the wrong fix."""
    msg = st._why_no_pairs(
        {
            "cuff_readings": 104,
            "sessions": 60,
            "unpaired": {"no_overlap": 100, "clock_never_read": 4},
            "nearest_miss_s": 4685.0,
        }
    )
    assert "100 readings fall outside" in msg
    assert "cuff sync" not in msg


def test_no_pairs_names_a_missing_cuff_clock_first():
    """A clock that was never read blocks pairing regardless of timing, so it
    outranks any complaint about when the reading was taken."""
    msg = st._why_no_pairs(
        {
            "cuff_readings": 6,
            "sessions": 2,
            "unpaired": {"clock_never_read": 5, "no_overlap": 1},
            "nearest_miss_s": 30.0,
        }
    )
    assert "cuff sync" in msg and "5" in msg


def test_no_pairs_distinguishes_a_clock_fault_from_mistimed_measurements():
    """A near-exact whole number of hours is a timezone or clock fault. Mistimed
    measurements miss by minutes. The two need opposite fixes, so the message must
    not be the same."""
    clock_fault = st._why_no_pairs(
        {
            "cuff_readings": 6,
            "sessions": 2,
            "unpaired": {"no_overlap": 6},
            "nearest_miss_s": 7205.0,
        }
    )
    assert "clock or timezone" in clock_fault and "2 h" in clock_fault

    mistimed = st._why_no_pairs(
        {
            "cuff_readings": 6,
            "sessions": 2,
            "unpaired": {"no_overlap": 6},
            "nearest_miss_s": 480.0,
        }
    )
    assert "while the recording is running" in mistimed
    assert "timezone" not in mistimed


def test_no_pairs_reports_the_obvious_cases_plainly():
    assert "No cuff readings" in st._why_no_pairs({"cuff_readings": 0, "sessions": 1})
    assert "No recordings" in st._why_no_pairs({"cuff_readings": 4, "sessions": 0})


def test_unpaired_readings_are_counted_by_reason(store):
    """One row per reason, each counted exactly once, so the totals can be trusted
    to add up to the readings that did not pair."""
    con = duckdb.connect(str(store))
    begin, end = NOW - 3600, NOW - 3000
    _insert_recording(con, sid=1, uuid="u1", start=begin, end=end, uploader="p1")
    tz = st.local_timezone(con)

    def wall(epoch):
        return con.execute(
            "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, epoch]
        ).fetchone()[0]

    # inside the recording with a good clock: pairs
    _insert_cuff(con, reading_id="a", taken_at=wall(begin + 60), uploader="p1")
    # three hours away with a good clock: no overlap
    _insert_cuff(con, reading_id="b", taken_at=wall(begin + 10800), uploader="p1")
    # inside, but no offset was ever measured
    _insert_cuff(con, reading_id="c", taken_at=wall(begin + 120), uploader="p1",
                 offset=None, valid=None)
    # inside, but the phone rejected the clock
    _insert_cuff(con, reading_id="d", taken_at=wall(begin + 180), uploader="p1", valid=False)
    # inside, but flagged as a suspect jump
    _insert_cuff(con, reading_id="e", taken_at=wall(begin + 240), uploader="p1", suspect=True)
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        out = st.collect(con, now=NOW)
    finally:
        con.close()
    s = out["subjects"][0]
    assert s["pairs"] == 1
    assert s["unpaired"] == {
        "no_overlap": 1,
        "clock_never_read": 1,
        "clock_invalid": 1,
        "clock_suspect": 1,
    }
    # The nearest miss describes the usable-clock reading that fell outside, and
    # its size is what separates a clock fault from a mistimed measurement.
    assert 10000 < s["nearest_miss_s"] < 11000
