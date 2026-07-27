"""Tests for viewer identity and per-subject data isolation.

These matter more than the rest of the suite: a mistake here shows one person's
medical data to another, and the failure is silent because the page looks fine.
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from ppg_pi_server import identity, series, status
from ppg_pi_server.auth import SCOPE_READ, add_token
from ppg_pi_server.config import Settings
from ppg_pi_server.ingest import Ingestor
from ppg_pi_server.main import app, get_ingestor
from ppg_pi_server.schema import init_audit_schema, init_cuff_schema

NOW = 1_785_000_000.0


# ---------------------------------------------------------------------------
# Token scoping
# ---------------------------------------------------------------------------


def test_token_without_subjects_sees_everything():
    """Tokens issued before scoping existed must keep working unchanged."""
    assert identity.subjects_from_token({"phone_id": "phone-01"}) == ("*",)


def test_token_subjects_are_read_as_a_tuple():
    assert identity.subjects_from_token({"subjects": ["a", "b"]}) == ("a", "b")
    assert identity.subjects_from_token({"subjects": "solo"}) == ("solo",)
    assert identity.subjects_from_token({"subjects": []}) == ()


def test_viewer_allows_only_its_own_subjects():
    v = identity.Viewer(name="her", subjects=("maggie-phone",), method="token")
    assert v.allowed("maggie-phone")
    assert not v.allowed("phone-01")
    assert not v.sees_all

    admin = identity.Viewer(name="me", subjects=("*",), method="token")
    assert admin.sees_all and admin.allowed("anything")


def test_unlisted_tailnet_login_is_denied_not_granted():
    """Deny by default. Being on the tailnet must not confer access to medical
    data belonging to someone else."""
    access = {"me@github": ["*"]}
    assert identity.viewer_for_login("stranger@example.com", access) is None
    assert identity.viewer_for_login(None, access) is None
    v = identity.viewer_for_login("me@github", access)
    assert v is not None and v.sees_all and v.method == "tailnet"


def test_empty_subject_list_yields_a_false_clause_not_an_open_one():
    """A viewer with an empty grant must match no rows. Getting this backwards
    would turn 'sees nothing' into 'sees everything'."""
    clause, params = status._subject_clause((), "uploader_phone_id")
    assert clause == "FALSE" and params == []
    clause, params = status._subject_clause(None, "uploader_phone_id")
    assert clause == "TRUE"
    clause, params = status._subject_clause(("a",), "uploader_phone_id")
    assert "IN (?)" in clause and params == ["a"]


# ---------------------------------------------------------------------------
# Isolation against a real store holding two subjects
# ---------------------------------------------------------------------------


@pytest.fixture
def two_subject_store(tmp_path):
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
    con.execute("CREATE TABLE ppg (session_id INTEGER, ts DOUBLE, v INTEGER)")
    tz = status.local_timezone(con)

    def wall(epoch):
        return con.execute(
            "SELECT strftime(timezone(?, to_timestamp(?)), '%Y-%m-%dT%H:%M:%S')", [tz, epoch]
        ).fetchone()[0]

    for sid, uuid, who, sys in ((1, "u-me", "phone-01", 120), (2, "u-her", "maggie-phone", 88)):
        start = NOW - 3600 * sid
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, 'Polar Sense X', 'AA', '{}', 0, 15)",
            [sid, uuid, start, start + 900],
        )
        con.execute(
            "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, device_name, "
            "opened_at, completed_at, status, files_json, convert_stats_json) "
            "VALUES (?, ?, 'Polar Sense X', ?, ?, 'complete', '[]', '{}')",
            [uuid, who, start, start + 900],
        )
        con.execute(
            "INSERT INTO cuff_readings (reading_id, taken_at, sys, dia, pulse, ihb, mov, "
            "device, uploader_phone_id, uploaded_at, phone_read_at, clock_offset_s, "
            "clock_offset_uncertainty_s, clock_valid, clock_suspect, slot) "
            "VALUES (?, ?, ?, 70, 70, FALSE, FALSE, NULL, ?, ?, NULL, 0.0, 1.0, TRUE, FALSE, 1)",
            [f"r{sid}", wall(start + 300), sys, who, NOW],
        )
        con.execute("INSERT INTO ppg VALUES (?, ?, 1)", [sid, start])
        con.execute(
            "INSERT INTO notes VALUES (?, ?, ?)",
            [sid, start + 10, '{"event":"calibration_start","name":"run"}'],
        )
    con.close()
    return db


def _collect(db, subjects):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return status.collect(con, now=NOW, subjects=subjects)
    finally:
        con.close()


def test_unrestricted_viewer_sees_both_subjects(two_subject_store):
    out = _collect(two_subject_store, ("*",))
    assert {s["subject_id"] for s in out["subjects"]} == {"phone-01", "maggie-phone"}
    assert out["totals"]["cuff_readings"] == 2
    assert out["totals"]["ppg_samples"] == 2
    assert len(out["markers"]) == 2


def test_restricted_viewer_sees_only_its_own_subject(two_subject_store):
    out = _collect(two_subject_store, ("maggie-phone",))
    assert {s["subject_id"] for s in out["subjects"]} == {"maggie-phone"}
    assert out["totals"]["cuff_readings"] == 1
    assert out["totals"]["sessions"] == 1
    assert out["totals"]["ppg_samples"] == 1
    assert out["totals"]["notes"] == 1
    assert len(out["markers"]) == 1
    # The other subject's readings must not appear anywhere in the payload.
    assert all(c["subject_id"] == "maggie-phone" for c in out["recent_cuff"])
    assert all(s["subject_id"] == "maggie-phone" for s in out["recent_sessions"])
    assert 120 not in [c["sys"] for c in out["recent_cuff"]]


def test_restricted_viewer_sees_no_other_subjects_series(two_subject_store):
    con = duckdb.connect(str(two_subject_store), read_only=True)
    try:
        tz = status.local_timezone(con)
        out = series.collect(con, timezone=tz, days=365, subjects=("maggie-phone",))
    finally:
        con.close()
    assert len(out["cuff"]) == 1
    assert out["cuff"][0]["sys"] == 88
    assert all(r["subject_id"] == "maggie-phone" for r in out["coverage"])
    # Quality cannot be attributed to a subject, so a restricted viewer gets none
    # rather than somebody else's figures.
    assert out["quality"] == []


def test_scoped_viewer_still_counts_its_own_pairs(two_subject_store):
    """Regression guard. A scoped pair query with its parameters in the wrong
    order reports zero pairs, which reads as 'no calibration data yet' rather
    than as a bug -- so only a scoped assertion catches it."""
    unrestricted = _collect(two_subject_store, ("*",))
    scoped = _collect(two_subject_store, ("maggie-phone",))
    hers_unrestricted = next(
        s["pairs"] for s in unrestricted["subjects"] if s["subject_id"] == "maggie-phone"
    )
    hers_scoped = scoped["subjects"][0]["pairs"]
    assert hers_scoped == hers_unrestricted == 1


def test_viewer_with_an_empty_grant_sees_nothing(two_subject_store):
    out = _collect(two_subject_store, ())
    assert out["subjects"] == []
    assert out["totals"]["cuff_readings"] == 0
    assert out["totals"]["ppg_samples"] == 0
    assert out["recent_cuff"] == [] and out["markers"] == []


def test_scope_is_reported_so_the_ui_can_say_what_is_shown(two_subject_store):
    assert _collect(two_subject_store, ("maggie-phone",))["scope"] == ["maggie-phone"]
    assert _collect(two_subject_store, ("*",))["scope"] == ["*"]


# ---------------------------------------------------------------------------
# End to end through HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(two_subject_store, tmp_path, monkeypatch):
    settings = Settings(
        db_path=two_subject_store,
        upload_dir=tmp_path / "up",
        tokens_file=tmp_path / "tokens.json",
    )
    monkeypatch.setattr("ppg_pi_server.config.Settings", lambda **kw: settings)
    all_token = add_token(settings, "operator", scope=SCOPE_READ)
    her_token = add_token(settings, "her-view", scope=SCOPE_READ, subjects=["maggie-phone"])
    ingestor = Ingestor(two_subject_store, tmp_path / "up")
    app.dependency_overrides[get_ingestor] = lambda: ingestor
    with TestClient(app) as c:
        yield c, all_token, her_token
    app.dependency_overrides.clear()


def test_scoped_token_over_http_returns_only_its_subject(client):
    c, all_token, her_token = client
    everything = c.get("/api/v1/status", headers={"Authorization": f"Bearer {all_token}"}).json()
    assert len(everything["subjects"]) == 2

    hers = c.get("/api/v1/status", headers={"Authorization": f"Bearer {her_token}"}).json()
    assert [s["subject_id"] for s in hers["subjects"]] == ["maggie-phone"]
    assert hers["scope"] == ["maggie-phone"]
    assert "phone-01" not in c.get(
        "/api/v1/status", headers={"Authorization": f"Bearer {her_token}"}
    ).text


def test_scoped_token_also_scopes_the_chart_series(client):
    c, _, her_token = client
    body = c.get(
        "/api/v1/series?days=365", headers={"Authorization": f"Bearer {her_token}"}
    ).json()
    assert len(body["cuff"]) == 1
    assert "phone-01" not in str(body)


def test_tailnet_identity_is_off_unless_enabled(client, monkeypatch):
    """With no token and the feature off, there is no way in."""
    c, _, _ = client
    assert c.get("/api/v1/status").status_code == 401


def test_whois_failure_is_not_an_error(monkeypatch):
    """An unidentifiable caller is simply unidentified. A crash here would take
    down the page for everybody, including the operator with a valid token."""
    identity.clear_cache()
    monkeypatch.setattr(identity.shutil, "which", lambda _: None)
    assert identity.whois("100.64.0.1:1234") is None
