"""Tests for the chart series layer."""

from __future__ import annotations

import duckdb
import pytest

from ppg_pi_server import series
from ppg_pi_server.schema import init_audit_schema, init_cuff_schema

NOW_ISH = 1_785_000_000.0


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
    con.execute(
        """
        CREATE TABLE derived_ppg_minute (
            ts DOUBLE, label VARCHAR, hr_ppg DOUBLE, sqi DOUBLE,
            perfusion DOUBLE, acc_motion DOUBLE, n_ppg INTEGER
        )
        """
    )
    con.close()
    return db


def _cuff(con, rid, taken_at, sys=120, dia=78, pulse=70, who="phone-01", offset=0.0,
          valid=True, suspect=False):
    con.execute(
        "INSERT INTO cuff_readings (reading_id, taken_at, sys, dia, pulse, ihb, mov, "
        "device, uploader_phone_id, uploaded_at, phone_read_at, clock_offset_s, "
        "clock_offset_uncertainty_s, clock_valid, clock_suspect, slot) "
        "VALUES (?, ?, ?, ?, ?, FALSE, FALSE, NULL, ?, ?, NULL, ?, 1.0, ?, ?, 1)",
        [rid, taken_at, sys, dia, pulse, who, NOW_ISH, offset, valid, suspect],
    )


def _session(con, sid, uuid, start, end, who="phone-01"):
    con.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 'Polar Sense X', 'AA', '{}', 0, 15)",
        [sid, uuid, start, end],
    )
    con.execute(
        "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, device_name, "
        "opened_at, completed_at, status, files_json, convert_stats_json) "
        "VALUES (?, ?, 'Polar Sense X', ?, ?, 'complete', '[]', '{}')",
        [uuid, who, start, end],
    )


def _tz(db):
    con = duckdb.connect(str(db))
    tz = series.local_timezone(con)
    con.close()
    return tz


def test_empty_store_returns_empty_series_not_an_error(store):
    con = duckdb.connect(str(store), read_only=True)
    try:
        out = series.collect(con, days=30)
    finally:
        con.close()
    assert out["cuff"] == [] and out["pairs"] == [] and out["quality"] == []
    assert out["coverage"] == []


def test_cuff_points_carry_subject_and_apply_the_clock_offset(store):
    con = duckdb.connect(str(store))
    tz = series.local_timezone(con)
    wall = con.execute(
        "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, NOW_ISH]
    ).fetchone()[0]
    _cuff(con, "a", wall, offset=0.0)
    _cuff(con, "b", wall, sys=95, offset=30.0, who="maggie-phone")
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        pts = series.cuff_points(con, tz=tz)
    finally:
        con.close()
    by_subject = {p["subject_id"]: p for p in pts}
    assert set(by_subject) == {"phone-01", "maggie-phone"}
    # The offset is cuff-minus-phone, so a cuff running 30 s fast corrects backwards.
    assert by_subject["maggie-phone"]["ts"] == pytest.approx(
        by_subject["phone-01"]["ts"] - 30.0, abs=1.0
    )


def test_quarantined_readings_are_excluded_from_charts(store):
    """A reading the phone quarantined should not be drawn as if it were fine."""
    con = duckdb.connect(str(store))
    tz = series.local_timezone(con)
    wall = con.execute(
        "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, NOW_ISH]
    ).fetchone()[0]
    _cuff(con, "ok", wall)
    _cuff(con, "bad", wall, suspect=True)
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        pts = series.cuff_points(con, tz=tz)
    finally:
        con.close()
    assert len(pts) == 1


def test_coverage_reports_minutes_and_cuff_counts_per_day_per_subject(store):
    con = duckdb.connect(str(store))
    tz = series.local_timezone(con)
    start = NOW_ISH - 3600
    _session(con, 1, "u-a", start, start + 600)  # 10 minutes
    wall = con.execute(
        "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, start]
    ).fetchone()[0]
    _cuff(con, "c1", wall)
    _cuff(con, "c2", wall.replace(":00", ":30"))
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        rows = series.coverage_days(con, tz=tz, days=30)
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0]["recorded_minutes"] == pytest.approx(10.0)
    assert rows[0]["cuff_count"] == 2
    assert rows[0]["subject_id"] == "phone-01"


def test_pairs_attach_a_ppg_heart_rate_only_when_minutes_overlap(store):
    con = duckdb.connect(str(store))
    tz = series.local_timezone(con)
    start, end = NOW_ISH - 1800, NOW_ISH
    _session(con, 1, "u-a", start, end)
    inside = start + 600
    wall = con.execute(
        "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, inside]
    ).fetchone()[0]
    _cuff(con, "paired", wall, pulse=72)
    con.execute(
        "INSERT INTO derived_ppg_minute VALUES (?, 'u-a', 70.5, 0.93, 0.1, 5.0, 4000)",
        [inside + 20],
    )
    # A minute far away must not be attached to this reading.
    con.execute(
        "INSERT INTO derived_ppg_minute VALUES (?, 'u-a', 140.0, 0.5, 0.1, 5.0, 4000)",
        [inside + 5000],
    )
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        pts = series.pair_points(con, tz=tz)
    finally:
        con.close()
    assert len(pts) == 1
    assert pts[0]["session_id"] == 1
    assert pts[0]["hr_ppg"] == pytest.approx(70.5)
    assert pts[0]["pulse"] == 72


def test_pairs_exclude_untrustworthy_clocks_and_other_subjects(store):
    con = duckdb.connect(str(store))
    tz = series.local_timezone(con)
    start, end = NOW_ISH - 1800, NOW_ISH
    _session(con, 1, "u-a", start, end, who="phone-01")
    wall = con.execute(
        "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, start + 300]
    ).fetchone()[0]
    _cuff(con, "bad-clock", wall, valid=False)
    _cuff(con, "other", wall, who="maggie-phone")
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        pts = series.pair_points(con, tz=tz)
    finally:
        con.close()
    assert pts == []


def test_quality_is_bucketed_hourly(store):
    con = duckdb.connect(str(store))
    base = NOW_ISH - (NOW_ISH % 3600)
    for i in range(5):
        con.execute(
            "INSERT INTO derived_ppg_minute VALUES (?, 'u-a', 70, ?, 0.1, 4.0, 4000)",
            [base + i * 60, 0.9],
        )
    con.execute(
        "INSERT INTO derived_ppg_minute VALUES (?, 'u-a', 70, 0.5, 0.1, 4.0, 4000)",
        [base + 3700],
    )
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        rows = series.quality_series(con, days=30)
    finally:
        con.close()
    assert len(rows) == 2
    assert rows[0]["minutes"] == 5
    assert rows[0]["sqi"] == pytest.approx(0.9)
    assert rows[1]["minutes"] == 1


def test_nan_values_do_not_break_the_payload(store):
    """Real derived_ppg_minute rows carry NaN where a minute had no usable pulse.
    json.dumps emits a bare NaN token, which is not valid JSON, so one bad minute
    would otherwise take out the entire chart response -- as it did on first
    deployment against the live store."""
    con = duckdb.connect(str(store))
    base = NOW_ISH - (NOW_ISH % 3600)
    con.execute(
        "INSERT INTO derived_ppg_minute VALUES (?, 'u-a', 'NaN'::DOUBLE, "
        "'NaN'::DOUBLE, 0.1, 4.0, 4000)",
        [base],
    )
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        rows = series.quality_series(con, days=30)
        payload = series.collect(con, days=30)
    finally:
        con.close()
    assert rows[0]["sqi"] is None and rows[0]["hr"] is None
    import json

    json.dumps(payload)  # must not raise


@pytest.mark.parametrize(
    "value,expected",
    [(1.234, 1.23), (None, None), (float("nan"), None), (float("inf"), None)],
)
def test_float_guard(value, expected):
    assert series._f(value, 2) == expected


def test_day_window_filters_old_readings(store):
    con = duckdb.connect(str(store))
    tz = series.local_timezone(con)
    recent = con.execute("SELECT strftime(now(), '%Y-%m-%dT%H:%M:%S')").fetchone()[0]
    _cuff(con, "recent", recent)
    _cuff(con, "ancient", "2020-01-01T10:00:00")
    con.close()

    con = duckdb.connect(str(store), read_only=True)
    try:
        assert len(series.cuff_points(con, tz=tz, days=7)) == 1
        assert len(series.cuff_points(con, tz=tz)) == 2
    finally:
        con.close()
