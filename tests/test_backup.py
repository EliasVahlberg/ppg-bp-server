"""Tests for the full/differential backup scheme.

Every scenario ends the same way: restore what the backup produced and compare
it, row for row, with the store it was taken from. Counting rows alone would
miss a restore that has the right number of the wrong rows.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from pathlib import Path

import duckdb
import pytest

from ppg_pi_server import backup
from ppg_pi_server._corelib import converter
from ppg_pi_server.schema import init_audit_schema, init_cuff_schema

# Midday UTC, so "a day earlier" is also a different local date in Europe/Stockholm.
T0 = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)


def _not_today(now: dt.datetime) -> int:
    """A full-weekday that is *not* the local weekday of ``now``, to force diffs."""
    return now.astimezone().date().isoweekday() % 7 + 1


# ---------------------------------------------------------------------------
# A small store with the real schema
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, root: Path) -> None:
        self.db = root / "live" / "sessions.duckdb"
        self.uploads = root / "live" / "uploads"
        self.tokens = root / "live" / "tokens.json"
        self.db.parent.mkdir(parents=True)
        self.uploads.mkdir()
        self.tokens.write_text('{"tok": {"phone_id": "p1"}}')
        with self.con() as con:
            converter.init_tables(con)
            init_audit_schema(con)
            init_cuff_schema(con)
            con.execute("CREATE TABLE derived_ppg_minute (ts DOUBLE, sqi DOUBLE)")

    def con(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.db))

    def add_session(self, uuid: str, n: int = 50, *, null_segment_rows: int = 0) -> int:
        """Insert a session the way the converter does: session, segments, rows."""
        with self.con() as con:
            sid = con.execute(
                "INSERT INTO sessions (session_uuid, start_time) VALUES (?, 1.0) "
                "RETURNING id", [uuid]).fetchone()[0]
            seg = con.execute(
                "INSERT INTO segments (session_id, local_segment_id) VALUES (?, 1) "
                "RETURNING id", [sid]).fetchone()[0]
            con.execute(
                "INSERT INTO ppg SELECT ?, ?, i, i, i+1, i+2, 0 FROM range(?) t(i)",
                [sid, seg, n])
            # Rows whose local segment was missing from segments.jsonl get a NULL
            # segment_id -- present on the live store (25,947 ppg rows on Oct 24).
            con.execute(
                "INSERT INTO ppg SELECT ?, NULL, 10000+i, i, i, i, 0 FROM range(?) t(i)",
                [sid, null_segment_rows])
            con.execute("INSERT INTO acc SELECT ?, ?, i, i, -i, 1 FROM range(?) t(i)",
                        [sid, seg, n * 2])
            con.execute(
                "INSERT INTO uploads (phone_session_uuid, status) VALUES (?, 'complete')",
                [uuid])
            con.execute("INSERT INTO derived_ppg_minute VALUES (?, 0.9)", [float(sid)])
        return sid

    def replace_session(self, uuid: str, n: int) -> int:
        """What _replace_existing_session does on a re-upload: delete, re-insert."""
        with self.con() as con:
            sid = con.execute("SELECT id FROM sessions WHERE session_uuid = ?",
                              [uuid]).fetchone()[0]
            for t in ("ppg", "acc", "gyro", "mag", "ppi", "notes", "segments"):
                con.execute(f"DELETE FROM {t} WHERE session_id = ?", [sid])
            con.execute("DELETE FROM sessions WHERE id = ?", [sid])
            con.execute("DELETE FROM uploads WHERE phone_session_uuid = ?", [uuid])
        return self.add_session(uuid, n)

    def delete_session(self, uuid: str) -> None:
        with self.con() as con:
            sid = con.execute("SELECT id FROM sessions WHERE session_uuid = ?",
                              [uuid]).fetchone()[0]
            for t in ("ppg", "acc", "segments"):
                con.execute(f"DELETE FROM {t} WHERE session_id = ?", [sid])
            con.execute("DELETE FROM sessions WHERE id = ?", [sid])


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


def _run(store: Store, root: Path, now: dt.datetime, **kw) -> backup.Snapshot | None:
    kw.setdefault("full_weekday", _not_today(now))
    return backup.run_backup(db_path=store.db, root=root, upload_dir=store.uploads,
                             tokens_file=store.tokens, now=now, **kw)


def _assert_identical(live: Path, restored: Path) -> None:
    """Same tables, and every table equal as a multiset of rows."""
    con = duckdb.connect()
    con.execute(f"ATTACH '{live}' AS l (READ_ONLY)")
    con.execute(f"ATTACH '{restored}' AS r (READ_ONLY)")
    tables = lambda db: {x[0] for x in con.execute(  # noqa: E731
        "SELECT table_name FROM duckdb_tables() WHERE database_name = ?", [db]).fetchall()}
    assert tables("l") == tables("r")
    for t in sorted(tables("l")):
        for a, b in (("l", "r"), ("r", "l")):
            extra = con.execute(
                f"SELECT count(*) FROM (SELECT * FROM {a}.{t} EXCEPT ALL "
                f"SELECT * FROM {b}.{t})").fetchone()[0]
            assert extra == 0, f"{t}: {extra} rows in {a} not in {b}"
    con.close()


# ---------------------------------------------------------------------------
# Full, diff, skip
# ---------------------------------------------------------------------------


def test_first_run_is_a_full_that_restores_exactly(store: Store, tmp_path: Path) -> None:
    store.add_session("a", null_segment_rows=3)
    root = tmp_path / "bk"
    snap = _run(store, root, T0)
    assert snap is not None and snap.kind == "full"
    assert (snap.path / backup.MANIFEST).is_file()
    assert (snap.path / "tokens.json").read_text() == store.tokens.read_text()

    out = tmp_path / "r.duckdb"
    backup.restore(snap.path, None, out)
    _assert_identical(store.db, out)


def test_diff_holds_only_new_sessions_and_restores_exactly(
    store: Store, tmp_path: Path
) -> None:
    store.add_session("a", n=100)
    root = tmp_path / "bk"
    full = _run(store, root, T0)
    # The new session includes NULL-segment rows: a segment_id-keyed diff would
    # silently drop exactly these.
    store.add_session("b", n=7, null_segment_rows=4)
    diff = _run(store, root, T0 + dt.timedelta(days=1))
    assert diff is not None and diff.kind == "diff"
    assert diff.manifest["base"] == full.stamp
    assert diff.manifest["files"]["ppg"] == 7 + 4, "diff must carry only session b"
    assert diff.manifest["files"]["acc"] == 14

    out = tmp_path / "r.duckdb"
    backup.restore(full.path, diff.path, out)
    _assert_identical(store.db, out)


def test_unchanged_store_is_skipped(store: Store, tmp_path: Path) -> None:
    store.add_session("a")
    root = tmp_path / "bk"
    _run(store, root, T0)
    assert _run(store, root, T0 + dt.timedelta(days=1)) is None
    assert len(backup.list_snapshots(root)[0]) == 1
    assert not (root / backup.INCOMING).exists()


def test_in_place_update_is_not_mistaken_for_unchanged(
    store: Store, tmp_path: Path
) -> None:
    """Same row counts, different content: uploads.status flips in place."""
    store.add_session("a")
    root = tmp_path / "bk"
    _run(store, root, T0)
    with store.con() as con:
        con.execute("UPDATE uploads SET status = 'error'")
    diff = _run(store, root, T0 + dt.timedelta(days=1))
    assert diff is not None, "an in-place update must produce a snapshot"
    out = tmp_path / "r.duckdb"
    backup.restore(backup.latest_restore_set(root)[0].path, diff.path, out)
    _assert_identical(store.db, out)


def test_token_change_alone_produces_a_snapshot(store: Store, tmp_path: Path) -> None:
    store.add_session("a")
    root = tmp_path / "bk"
    _run(store, root, T0)
    store.tokens.write_text('{"tok2": {"phone_id": "p2"}}')
    snap = _run(store, root, T0 + dt.timedelta(days=1))
    assert snap is not None
    assert (snap.path / "tokens.json").read_text() == store.tokens.read_text()


def test_replaced_and_deleted_sessions_restore_exactly(
    store: Store, tmp_path: Path
) -> None:
    """The cases a 'rows since' export cannot carry by itself."""
    store.add_session("keep", n=20)
    store.add_session("reupload", n=20)
    store.add_session("dropped", n=20)
    root = tmp_path / "bk"
    full = _run(store, root, T0)
    store.replace_session("reupload", n=33)  # new id, old rows gone
    store.delete_session("dropped")
    store.add_session("new", n=5)
    diff = _run(store, root, T0 + dt.timedelta(days=1))
    assert diff is not None and diff.kind == "diff"

    out = tmp_path / "r.duckdb"
    backup.restore(full.path, diff.path, out)
    _assert_identical(store.db, out)


def test_diffs_are_against_the_full_not_the_previous_diff(
    store: Store, tmp_path: Path
) -> None:
    store.add_session("a")
    root = tmp_path / "bk"
    full = _run(store, root, T0)
    store.add_session("b", n=3)
    _run(store, root, T0 + dt.timedelta(days=1))
    store.add_session("c", n=4)
    d2 = _run(store, root, T0 + dt.timedelta(days=2))
    assert d2.manifest["base"] == full.stamp
    assert d2.manifest["files"]["ppg"] == 3 + 4, "carries b and c, so full + d2 suffices"
    out = tmp_path / "r.duckdb"
    backup.restore(full.path, d2.path, out)
    _assert_identical(store.db, out)


def test_restored_sequences_do_not_collide(store: Store, tmp_path: Path) -> None:
    """After a diff restore, the next session id must be past the diff's ids."""
    store.add_session("a")
    root = tmp_path / "bk"
    full = _run(store, root, T0)
    store.add_session("b")
    store.add_session("c")
    diff = _run(store, root, T0 + dt.timedelta(days=1))
    out = tmp_path / "r.duckdb"
    backup.restore(full.path, diff.path, out)
    con = duckdb.connect(str(out))
    top = con.execute("SELECT max(id) FROM sessions").fetchone()[0]
    new = con.execute("INSERT INTO sessions (session_uuid) VALUES ('z') RETURNING id"
                      ).fetchone()[0]
    seg_top = con.execute("SELECT max(id) FROM segments").fetchone()[0]
    new_seg = con.execute(
        "INSERT INTO segments (session_id) VALUES (1) RETURNING id").fetchone()[0]
    con.close()
    assert new > top and new_seg > seg_top


def test_restore_works_after_the_snapshot_is_moved(store: Store, tmp_path: Path) -> None:
    """load.sql holds absolute paths; a pulled copy must still restore."""
    store.add_session("a", n=9)
    root = tmp_path / "bk"
    full = _run(store, root, T0)
    moved = tmp_path / "elsewhere" / full.stamp
    shutil.move(str(full.path), moved)
    out = tmp_path / "r.duckdb"
    backup.restore(moved, None, out)
    _assert_identical(store.db, out)


def test_restore_rejects_a_diff_for_another_full(store: Store, tmp_path: Path) -> None:
    store.add_session("a")
    root = tmp_path / "bk"
    f1 = _run(store, root, T0)
    store.add_session("b")
    f2 = _run(store, root, T0 + dt.timedelta(days=1), force_full=True)
    store.add_session("c")
    d = _run(store, root, T0 + dt.timedelta(days=2))
    assert d.manifest["base"] == f2.stamp
    with pytest.raises(ValueError, match="based on"):
        backup.restore(f1.path, d.path, tmp_path / "r.duckdb")


# ---------------------------------------------------------------------------
# When a full is taken
# ---------------------------------------------------------------------------


def _snap(root: Path, stamp: str, kind: str, base: str | None = None) -> backup.Snapshot:
    d = root / stamp
    if kind == "legacy-full":
        (d / backup.FULL_DIR).mkdir(parents=True)
        (d / backup.FULL_DIR / "schema.sql").write_text("")
        (d / backup.FULL_DIR / "load.sql").write_text("")
        return backup.Snapshot(d, kind, None)
    d.mkdir(parents=True)
    m = {"type": kind, "base": base, "max_session_id": 0}
    (d / backup.MANIFEST).write_text(json.dumps(m))
    return backup.Snapshot(d, kind, m)


def test_full_due_rules(tmp_path: Path) -> None:
    now = T0
    wd = now.astimezone().date().isoweekday()
    assert backup._full_due([], now, wd) is not None
    recent = _snap(tmp_path, (now - dt.timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ"),
                   "full")
    assert backup._full_due([recent], now, _not_today(now)) is None
    assert backup._full_due([recent], now, wd) == "weekly full day"
    old = _snap(tmp_path, (now - dt.timedelta(days=8)).strftime("%Y%m%dT%H%M%SZ"),
                "full")
    assert "days old" in backup._full_due([old], now, _not_today(now))
    legacy = _snap(tmp_path, "20261024T220748Z", "legacy-full")
    assert "predates manifests" in backup._full_due([legacy], now, _not_today(now))


def test_full_day_does_not_take_two_fulls(store: Store, tmp_path: Path) -> None:
    """A manual re-run on the full day is a diff, not a second 12 GB full."""
    store.add_session("a")
    root = tmp_path / "bk"
    wd = T0.astimezone().date().isoweekday()
    assert _run(store, root, T0, full_weekday=wd).kind == "full"
    store.add_session("b")
    assert _run(store, root, T0 + dt.timedelta(minutes=5), full_weekday=wd).kind == "diff"


# ---------------------------------------------------------------------------
# Retention and crash safety
# ---------------------------------------------------------------------------


def test_retention_keeps_five_fulls_and_only_their_diffs(tmp_path: Path) -> None:
    snaps = [
        _snap(tmp_path, "20261001T000000Z", "legacy-full"),
        _snap(tmp_path, "20261002T000000Z", "legacy-full"),
        _snap(tmp_path, "20261004T000000Z", "full"),
        _snap(tmp_path, "20261005T000000Z", "diff", base="20261004T000000Z"),
        _snap(tmp_path, "20261011T000000Z", "full"),
        _snap(tmp_path, "20261012T000000Z", "diff", base="20261011T000000Z"),
        _snap(tmp_path, "20261018T000000Z", "full"),
        _snap(tmp_path, "20261025T000000Z", "full"),
        _snap(tmp_path, "20261026T000000Z", "diff", base="20261025T000000Z"),
    ]
    doomed = {s.stamp for s in backup.plan_prune(snaps, keep_full=5)}
    assert doomed == {"20261001T000000Z"}
    doomed = {s.stamp for s in backup.plan_prune(snaps, keep_full=3)}
    assert doomed == {"20261001T000000Z", "20261002T000000Z",
                      "20261004T000000Z", "20261005T000000Z"}


def test_prune_never_touches_unrecognised_directories(tmp_path: Path) -> None:
    for i in range(7):
        _snap(tmp_path, f"2026101{i}T000000Z", "full")
    stray = tmp_path / "20261020T000000Z"
    stray.mkdir()  # no manifest, no finished export: incomplete, not ours to judge
    (tmp_path / "notes").mkdir()
    backup.prune(tmp_path, keep_full=5)
    assert stray.is_dir() and (tmp_path / "notes").is_dir()
    assert len(backup.list_snapshots(tmp_path)[0]) == 5


def test_interrupted_run_leaves_no_snapshot_and_is_cleaned_up(
    store: Store, tmp_path: Path, monkeypatch
) -> None:
    store.add_session("a")
    root = tmp_path / "bk"

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backup, "_copy_uploads", boom)
    with pytest.raises(RuntimeError):
        _run(store, root, T0)
    assert backup.list_snapshots(root) == ([], [])
    assert (root / backup.INCOMING).exists()
    monkeypatch.undo()

    snap = _run(store, root, T0 + dt.timedelta(minutes=1))
    assert snap is not None and not (root / backup.INCOMING).exists()


def test_legacy_snapshot_forces_a_new_full_first(store: Store, tmp_path: Path) -> None:
    """The first run after upgrading cannot diff against a manifest-less export."""
    store.add_session("a")
    root = tmp_path / "bk"
    legacy = root / "20261020T000000Z" / backup.FULL_DIR
    legacy.parent.mkdir(parents=True)
    with store.con() as con:
        con.execute(f"EXPORT DATABASE '{legacy}' (FORMAT PARQUET)")
    snaps, _ = backup.list_snapshots(root)
    assert [s.kind for s in snaps] == ["legacy-full"]
    snap = _run(store, root, T0)
    assert snap.kind == "full"
    # And the legacy export itself is still restorable.
    out = tmp_path / "r.duckdb"
    backup.restore(legacy.parent, None, out)
    _assert_identical(store.db, out)


def test_verify_detects_a_damaged_snapshot(store: Store, tmp_path: Path) -> None:
    store.add_session("a", n=10)
    root = tmp_path / "bk"
    snap = _run(store, root, T0)
    backup.verify(snap.path)
    m = json.loads((snap.path / backup.MANIFEST).read_text())
    m["files"]["ppg"] += 1
    (snap.path / backup.MANIFEST).write_text(json.dumps(m))
    with pytest.raises(RuntimeError, match="manifest"):
        backup.verify(snap.path)


def test_cli_run_and_restore_check(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "bk"
    store.add_session("a")
    args = ["--root", str(root), "run", "--db", str(store.db), "--uploads",
            str(store.uploads), "--tokens", str(store.tokens)]
    assert backup.main(args) == 0
    time.sleep(1.1)  # snapshot names have one-second resolution
    store.add_session("b")
    assert backup.main(args + ["--full-weekday", str(_not_today(
        dt.datetime.now(dt.timezone.utc)))]) == 0
    assert backup.main(["--root", str(root), "restore-check",
                        "--scratch", str(tmp_path / "scratch")]) == 0
    assert not list((tmp_path / "scratch").glob("*.duckdb"))
