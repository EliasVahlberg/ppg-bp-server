"""Weekly-full / daily-differential snapshots of the server's data.

Replaces a scheme that took a full ~12 GB Parquet export every night and kept
14 of them (175 GB), re-exported identical data on nights nothing arrived, and
ran DuckDB with its defaults (80 % of RAM, all cores), peaking near 10 GB RSS.

Layout under the backup root, one directory per snapshot, named by UTC stamp::

    <stamp>/manifest.json     written LAST; a directory without it is incomplete
    <stamp>/tokens.json
    <stamp>/uploads/          raw bundles, hardlinked against the previous snapshot
    <stamp>/duckdb-export/    full:  EXPORT DATABASE (schema.sql, load.sql, *.parquet)
    <stamp>/duckdb-diff/      diff:  one <table>.parquet per table

Snapshots are built in ``<root>/.incoming/<stamp>`` and renamed into place, so a
crash never leaves something that looks like a snapshot.

**What a differential holds.** Always relative to the newest *full*, so a restore
is the full plus one diff, never a chain.

* The five sensor tables (``BIG_TABLES``, ~all of the ~1.15 billion rows) are
  exported only for ``session_id > base.max_session_id``.
* Every other table is small and mutable (``uploads.status``, ``sessions.end_time``,
  the recomputed ``derived_*``), so it is copied in full.

Keyed on ``session_id``, not ``segment_id``, because of what the data actually
contains (checked against the Oct 11/18/24 snapshots before writing this):

* ``segment_id`` is NULL for samples whose local segment is missing from
  ``segments.jsonl`` -- 25,947 ppg rows and ~63k each in acc/gyro, 15k of them
  added in the week to Oct 24. A ``segment_id > X`` filter drops those silently.
* ``session_id`` is never NULL, every row belongs to a ``sessions`` row, and
  sessions are immutable once inserted: the converter replaces a re-uploaded
  session by *deleting* it and inserting it again under a new, higher id
  (``_replace_existing_session``). No surviving session changed its row count
  between the snapshots checked, and every new id exceeded the previous max.

Deletions are therefore the one thing a "rows since" export cannot carry. The
restore handles them: any session present in the full but absent from the diff's
``sessions`` copy is deleted from the sensor tables before the diff rows go in.

**Resources.** One read-only connection with a capped ``memory_limit`` and
``threads``, spilling to a temp directory on the backup disk. The systemd unit
adds ``MemoryMax`` as a backstop. The export also takes the cross-process store
lock (see storelock), because a conversion that starts mid-export cannot open
the store read-write and would fail after the phone has been told 200.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .storelock import store_write_lock

logger = logging.getLogger("ppg_pi_server.backup")

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
FULL_DIR = "duckdb-export"
DIFF_DIR = "duckdb-diff"
INCOMING = ".incoming"

#: Append-only per session; carry ~all of the rows. Diffed by session_id.
BIG_TABLES = ("ppg", "acc", "gyro", "mag", "ppi")

DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 2
#: A full is due once the newest one is at least this old, whatever the weekday,
#: so a skipped or missed full-day does not push the base back indefinitely.
FULL_MAX_AGE = dt.timedelta(days=7)

_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")


# ---------------------------------------------------------------------------
# Snapshot inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    path: Path
    kind: str  # "full" | "diff" | "legacy-full"
    manifest: dict[str, Any] | None

    @property
    def stamp(self) -> str:
        return self.path.name

    @property
    def is_full(self) -> bool:
        return self.kind in ("full", "legacy-full")

    @property
    def created(self) -> dt.datetime:
        return dt.datetime.strptime(self.stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc
        )


def list_snapshots(root: Path) -> tuple[list[Snapshot], list[Path]]:
    """Return (complete snapshots oldest-first, directories not recognised).

    ``legacy-full`` is a snapshot from the previous script: no manifest, but a
    finished ``EXPORT DATABASE`` (schema.sql and load.sql are written at the end).
    They are valid restore points and count as fulls for retention. Anything else
    without a manifest is incomplete and is reported, never touched.
    """
    snaps: list[Snapshot] = []
    unknown: list[Path] = []
    if not root.is_dir():
        return snaps, unknown
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name.startswith(".") or not _STAMP_RE.match(d.name):
            continue
        mf = d / MANIFEST
        if mf.is_file():
            m = json.loads(mf.read_text())
            snaps.append(Snapshot(d, m["type"], m))
        elif (d / FULL_DIR / "load.sql").is_file() and (d / FULL_DIR / "schema.sql").is_file():
            snaps.append(Snapshot(d, "legacy-full", None))
        else:
            unknown.append(d)
    return snaps, unknown


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _stat(p: Path) -> tuple[int, int]:
    try:
        st = p.stat()
    except FileNotFoundError:
        return (0, 0)
    return (st.st_size, st.st_mtime_ns)


def _uploads_fingerprint(upload_dir: Path | None) -> list[int]:
    n = total = newest = 0
    if upload_dir and upload_dir.is_dir():
        for dirpath, _dirs, files in os.walk(upload_dir):
            for f in files:
                st = os.stat(os.path.join(dirpath, f))
                n += 1
                total += st.st_size
                newest = max(newest, st.st_mtime_ns)
    return [n, total, newest]


def _sha256(p: Path | None) -> str | None:
    if p is None or not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = current_database() AND schema_name = 'main' "
        "AND NOT temporary ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def fingerprint(
    con: duckdb.DuckDBPyConnection,
    db_path: Path,
    upload_dir: Path | None,
    tokens_file: Path | None,
) -> dict[str, Any]:
    """Everything that, if unchanged, makes a new snapshot pointless.

    Content, not file metadata. The database file's mtime/size was the obvious
    signal but is the wrong one: committed changes can sit in the WAL without
    touching the main file, and a read-write open that changes nothing (the
    dashboard's incremental refresh, which mostly reports "unchanged") can still
    checkpoint and move the mtime -- which would defeat skipping entirely.

    Instead: row counts and max ids for every table, plus an order-independent
    hash of every row of the small tables, where in-place updates happen
    (``uploads.status``, ``sessions.end_time``, recomputed ``derived_*``). The
    sensor tables need only counts, because their rows only ever change by a
    whole session being deleted and re-inserted, which moves counts and
    ``sessions``. Plus the raw uploads tree and the tokens file.
    """
    tables = list_tables(con)
    counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    # sum() of 64-bit row hashes as HUGEINT: order-independent, and unlike
    # bit_xor a duplicated row does not cancel itself out.
    hashes = {
        t: str(con.execute(
            f'SELECT coalesce(sum(hash(r)::HUGEINT), 0) FROM (SELECT * FROM "{t}") r'
        ).fetchone()[0])
        for t in tables if t not in BIG_TABLES
    }
    max_session = (
        con.execute("SELECT coalesce(max(id), 0) FROM sessions").fetchone()[0]
        if "sessions" in tables else 0
    )
    max_segment = (
        con.execute("SELECT coalesce(max(id), 0) FROM segments").fetchone()[0]
        if "segments" in tables else 0
    )
    return {
        "max_session_id": int(max_session),
        "max_segment_id": int(max_segment),
        "row_counts": counts,
        "small_table_hashes": hashes,
        "uploads": _uploads_fingerprint(upload_dir),
        "tokens_sha256": _sha256(tokens_file),
    }


# ---------------------------------------------------------------------------
# Taking a snapshot
# ---------------------------------------------------------------------------


def _connect_limited(
    db: Path | str, *, read_only: bool, memory_limit: str, threads: int, temp_dir: Path
) -> duckdb.DuckDBPyConnection:
    temp_dir.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(
        str(db),
        read_only=read_only,
        config={
            "memory_limit": memory_limit,
            "threads": threads,
            "temp_directory": str(temp_dir),
        },
    )


def _parquet_count(p: Path) -> int:
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        return con.execute("SELECT count(*) FROM read_parquet(?)", [str(p)]).fetchone()[0]
    finally:
        con.close()


def _sql_str(p: Path) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def _full_due(fulls: list[Snapshot], now: dt.datetime, full_weekday: int) -> str | None:
    """Why a full is needed now, or None for a diff."""
    if not fulls:
        return "no full snapshot exists"
    newest = fulls[-1]
    if newest.kind == "legacy-full":
        return "newest full predates manifests; a diff needs a manifest-bearing base"
    age = now - newest.created
    if age >= FULL_MAX_AGE:
        return f"newest full is {age.days} days old"
    local_today = now.astimezone().date()
    if (local_today.isoweekday() == full_weekday
            and newest.created.astimezone().date() != local_today):
        return "weekly full day"
    return None


def _copy_uploads(upload_dir: Path, dest: Path, prev: Snapshot | None) -> None:
    if not upload_dir.is_dir():
        return
    cmd = ["rsync", "-a"]
    if prev is not None and (prev.path / "uploads").is_dir():
        cmd.append(f"--link-dest={prev.path / 'uploads'}")
    cmd += [f"{upload_dir}/", f"{dest / 'uploads'}/"]
    subprocess.run(cmd, check=True)


def run_backup(
    *,
    db_path: Path,
    root: Path,
    upload_dir: Path | None,
    tokens_file: Path | None,
    full_weekday: int = 7,
    force_full: bool = False,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    threads: int = DEFAULT_THREADS,
    now: dt.datetime | None = None,
) -> Snapshot | None:
    """Take a full or differential snapshot, or skip. Returns the new snapshot."""
    now = now or dt.datetime.now(dt.timezone.utc)
    root.mkdir(parents=True, exist_ok=True)
    incoming = root / INCOMING
    if incoming.exists():
        # Only this function writes here, and only one run happens at a time
        # (oneshot unit), so anything left is a crashed run's partial output.
        logger.warning("removing partial snapshot(s) from an interrupted run: %s",
                       sorted(p.name for p in incoming.iterdir()))
        shutil.rmtree(incoming)

    snaps, unknown = list_snapshots(root)
    for u in unknown:
        logger.warning("ignoring %s: no manifest and no finished export", u.name)
    fulls = [s for s in snaps if s.is_full]
    newest = snaps[-1] if snaps else None

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    if (root / stamp).exists():
        raise FileExistsError(f"snapshot {stamp} already exists")
    stage = incoming / stamp
    tmp = root / ".duckdb-tmp"

    with store_write_lock(db_path, label="backup"):
        con = _connect_limited(db_path, read_only=True, memory_limit=memory_limit,
                               threads=threads, temp_dir=tmp)
        try:
            con.execute("BEGIN TRANSACTION")
            fp = fingerprint(con, db_path, upload_dir, tokens_file)

            if (not force_full and newest is not None and newest.manifest
                    and newest.manifest.get("fingerprint") == fp):
                con.execute("COMMIT")
                logger.info("unchanged since %s, skipped", newest.stamp)
                return None

            reason = "forced" if force_full else _full_due(fulls, now, full_weekday)
            kind = "full" if reason else "diff"
            stage.mkdir(parents=True)
            t0 = time.monotonic()
            manifest: dict[str, Any] = {
                "format_version": FORMAT_VERSION,
                "type": kind,
                "stamp": stamp,
                "created_at": now.isoformat(),
                "duckdb_version": duckdb.__version__,
                "fingerprint": fp,
                # Informational only (see fingerprint() for why it is not compared).
                "db_file": {"size_mtime_ns": list(_stat(db_path)),
                            "wal_size_mtime_ns": list(_stat(Path(f"{db_path}.wal")))},
                "max_session_id": fp["max_session_id"],
                "max_segment_id": fp["max_segment_id"],
                "row_counts": fp["row_counts"],
                "session_ids": [r[0] for r in con.execute(
                    "SELECT id FROM sessions ORDER BY id").fetchall()],
            }
            if kind == "full":
                logger.info("taking a FULL snapshot (%s)", reason)
                out = stage / FULL_DIR
                con.execute(f"EXPORT DATABASE {_sql_str(out)} (FORMAT PARQUET)")
                manifest["files"] = {
                    p.stem: _parquet_count(p) for p in sorted(out.glob("*.parquet"))
                }
            else:
                base = fulls[-1]
                assert base.manifest is not None
                base_max = int(base.manifest["max_session_id"])
                logger.info("taking a DIFF snapshot against %s (sessions > %d)",
                            base.stamp, base_max)
                out = stage / DIFF_DIR
                out.mkdir()
                files = {}
                for t in list_tables(con):
                    target = out / f"{t}.parquet"
                    if t in BIG_TABLES:
                        sel = f'SELECT * FROM "{t}" WHERE session_id > {base_max}'
                    else:
                        sel = f'SELECT * FROM "{t}"'
                    con.execute(f"COPY ({sel}) TO {_sql_str(target)} (FORMAT PARQUET)")
                    files[t] = _parquet_count(target)
                manifest.update(base=base.stamp, base_max_session_id=base_max,
                                files=files)
            con.execute("COMMIT")
        finally:
            con.close()
    shutil.rmtree(tmp, ignore_errors=True)

    # The data files must exactly match the counts read inside the transaction;
    # anything else is a broken export, and must not become a restore point.
    _check_files_against_counts(manifest)

    if tokens_file is not None and tokens_file.is_file():
        shutil.copy2(tokens_file, stage / "tokens.json")
        os.chmod(stage / "tokens.json", 0o600)
    if upload_dir is not None:
        _copy_uploads(upload_dir, stage, newest)

    manifest["export_seconds"] = round(time.monotonic() - t0, 1)
    (stage / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    final = root / stamp
    os.rename(stage, final)
    os.rmdir(incoming)
    logger.info("%s snapshot complete: %s (%.0fs)", kind, final, manifest["export_seconds"])
    return Snapshot(final, kind, manifest)


def _check_files_against_counts(manifest: dict[str, Any]) -> None:
    counts = manifest["row_counts"]
    files = manifest["files"]
    if set(files) != set(counts):
        raise RuntimeError(f"exported tables {sorted(files)} != live tables {sorted(counts)}")
    if manifest["type"] == "full":
        bad = {t: (files[t], counts[t]) for t in counts if files[t] != counts[t]}
    else:
        bad = {t: (files[t], counts[t]) for t in counts
               if t not in BIG_TABLES and files[t] != counts[t]}
    if bad:
        raise RuntimeError(f"export row counts disagree with the store: {bad}")


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def plan_prune(snaps: list[Snapshot], keep_full: int) -> list[Snapshot]:
    """Snapshots to delete: fulls beyond the newest ``keep_full`` and every diff
    whose base is not kept.

    Only complete snapshots are considered, so a full is only ever dropped once
    ``keep_full`` newer fulls have *completed* -- an interrupted full never
    displaces a good one.
    """
    if keep_full < 1:
        raise ValueError("keep_full must be >= 1")
    fulls = [s for s in snaps if s.is_full]
    kept = {s.stamp for s in fulls[-keep_full:]}
    doomed = [s for s in fulls if s.stamp not in kept]
    doomed += [s for s in snaps if s.kind == "diff"
               and (s.manifest or {}).get("base") not in kept]
    return sorted(doomed, key=lambda s: s.stamp)


def prune(root: Path, keep_full: int, *, dry_run: bool = False) -> list[Snapshot]:
    snaps, _ = list_snapshots(root)
    doomed = plan_prune(snaps, keep_full)
    for s in doomed:
        logger.info("%s %s snapshot %s", "would delete" if dry_run else "deleting",
                    s.kind, s.stamp)
        if not dry_run:
            shutil.rmtree(s.path)
    return doomed


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

_LOAD_RE = re.compile(r"^COPY\s+(\S+)\s+FROM\s+'([^']+)'(.*)$", re.IGNORECASE)


def _load_statements(export_dir: Path) -> list[str]:
    """load.sql with every path re-pointed at ``export_dir``.

    EXPORT DATABASE writes absolute paths, so a snapshot that has been moved or
    pulled to another machine would otherwise load from where it *used* to be.
    """
    out = []
    for line in (export_dir / "load.sql").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LOAD_RE.match(line)
        if not m:
            raise RuntimeError(f"unrecognised load.sql line: {line}")
        table, path, rest = m.groups()
        local = export_dir / Path(path).name
        out.append(f"COPY {table} FROM {_sql_str(local)}{rest}")
    return out


def _advance_sequences(con: duckdb.DuckDBPyConnection) -> None:
    """Move each id sequence past the ids the diff inserted.

    The sequences come from the full's schema.sql, so after diff rows are added
    their next value would collide with existing primary keys.
    """
    cols = con.execute(
        "SELECT table_name, column_name, column_default FROM duckdb_columns() "
        "WHERE database_name = current_database() AND column_default LIKE 'nextval(%'"
    ).fetchall()
    for table, column, default in cols:
        m = re.search(r"nextval\('([^']+)'\)", default)
        if not m:
            continue
        seq = m.group(1)
        top = con.execute(f'SELECT coalesce(max("{column}"), 0) FROM "{table}"').fetchone()[0]
        last = con.execute(
            "SELECT coalesce(last_value, start_value - 1) FROM duckdb_sequences() "
            "WHERE sequence_name = ?", [seq]).fetchone()[0]
        if top > last:
            con.execute(f"SELECT max(nextval('{seq}')) FROM range({int(top - last)})")


def restore(
    full: Path,
    diff: Path | None,
    out_db: Path,
    *,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    threads: int = DEFAULT_THREADS,
) -> dict[str, int]:
    """Rebuild a store from a full snapshot plus optionally one diff.

    Returns the restored per-table row counts, after checking them against the
    manifest of the last snapshot applied.
    """
    if out_db.exists():
        raise FileExistsError(f"{out_db} exists; restore only into a new file")
    full_m = _read_manifest(full)
    diff_m = _read_manifest(diff) if diff else None
    if diff_m is not None:
        if diff_m["type"] != "diff":
            raise ValueError(f"{diff} is not a diff snapshot")
        if diff_m["base"] != full.name:
            raise ValueError(f"{diff.name} is based on {diff_m['base']}, not {full.name}")

    export_dir = full / FULL_DIR
    con = _connect_limited(out_db, read_only=False, memory_limit=memory_limit,
                           threads=threads, temp_dir=out_db.parent / f".{out_db.name}.tmp")
    try:
        con.execute((export_dir / "schema.sql").read_text())
        for stmt in _load_statements(export_dir):
            con.execute(stmt)
        if full_m is not None:
            _expect(con, full_m["row_counts"], f"full {full.name}")

        if diff is not None and diff_m is not None:
            ddir = diff / DIFF_DIR
            base_ids = set(full_m["session_ids"]) if full_m else {
                r[0] for r in con.execute("SELECT id FROM sessions").fetchall()}
            existing = set(list_tables(con))
            con.execute("BEGIN TRANSACTION")
            for t in diff_m["files"]:
                if t in BIG_TABLES:
                    continue
                src = _sql_str(ddir / f"{t}.parquet")
                if t in existing:
                    con.execute(f'DELETE FROM "{t}"')
                    con.execute(f'INSERT INTO "{t}" SELECT * FROM read_parquet({src})')
                else:
                    logger.warning("table %s is newer than the full; created without "
                                   "constraints", t)
                    con.execute(f'CREATE TABLE "{t}" AS SELECT * FROM read_parquet({src})')
            now_ids = {r[0] for r in con.execute("SELECT id FROM sessions").fetchall()}
            removed = sorted(base_ids - now_ids)
            if removed:
                logger.info("sessions deleted or replaced since the full: %s", removed)
            for t in BIG_TABLES:
                if t not in diff_m["files"]:
                    continue
                if removed:
                    con.execute(
                        f'DELETE FROM "{t}" WHERE session_id IN '
                        f'(SELECT unnest(?::INTEGER[]))', [removed])
                src = _sql_str(ddir / f"{t}.parquet")
                if t in existing:
                    con.execute(f'INSERT INTO "{t}" SELECT * FROM read_parquet({src})')
                else:
                    con.execute(f'CREATE TABLE "{t}" AS SELECT * FROM read_parquet({src})')
            con.execute("COMMIT")
            _advance_sequences(con)
            _expect(con, diff_m["row_counts"], f"diff {diff.name}")
        counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                  for t in list_tables(con)}
    finally:
        con.close()
    return counts


def _read_manifest(snap: Path | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    mf = snap / MANIFEST
    return json.loads(mf.read_text()) if mf.is_file() else None


def _expect(con: duckdb.DuckDBPyConnection, expected: dict[str, int], what: str) -> None:
    actual = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
              for t in list_tables(con)}
    if actual != expected:
        diff = {t: (actual.get(t), expected.get(t))
                for t in set(actual) | set(expected) if actual.get(t) != expected.get(t)}
        raise RuntimeError(f"restore of {what} does not match its manifest "
                           f"(restored, expected): {diff}")
    logger.info("restored %s: %d tables, %s rows, matches manifest", what,
                len(actual), f"{sum(actual.values()):,}")


def verify(snap: Path) -> dict[str, int]:
    """Re-count every data file and compare with the manifest."""
    m = _read_manifest(snap)
    sub = snap / (DIFF_DIR if m and m["type"] == "diff" else FULL_DIR)
    counts = {p.stem: _parquet_count(p) for p in sorted(sub.glob("*.parquet"))}
    if m is not None and counts != m["files"]:
        raise RuntimeError(f"{snap.name}: files {counts} != manifest {m['files']}")
    return counts


def latest_restore_set(root: Path) -> tuple[Snapshot, Snapshot | None]:
    """The newest restorable point: newest full, plus its newest diff if any."""
    snaps, _ = list_snapshots(root)
    fulls = [s for s in snaps if s.is_full]
    if not fulls:
        raise RuntimeError(f"no full snapshot under {root}")
    full = fulls[-1]
    diffs = [s for s in snaps if s.kind == "diff" and s.manifest
             and s.manifest.get("base") == full.stamp]
    return full, (diffs[-1] if diffs else None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)
    data = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    p = argparse.ArgumentParser(prog="python -m ppg_pi_server.backup")
    p.add_argument("--root", type=Path,
                   default=_env_path("PPG_PI_BACKUP_DIR", data / "ppg-pi-server-backups"))
    p.add_argument("--memory-limit", default=DEFAULT_MEMORY_LIMIT)
    p.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="take a full or diff snapshot, or skip, then prune")
    r.add_argument("--db", type=Path, default=_env_path(
        "PPG_PI_SERVER_DB_PATH", data / "ppg-pi-server/sessions.duckdb"))
    r.add_argument("--uploads", type=Path, default=_env_path(
        "PPG_PI_SERVER_UPLOAD_DIR", data / "ppg-pi-server/uploads"))
    r.add_argument("--tokens", type=Path, default=_env_path(
        "PPG_PI_SERVER_TOKENS_FILE", data / "ppg-pi-server/tokens.json"))
    r.add_argument("--keep-full", type=int, default=5)
    r.add_argument("--full-weekday", type=int, default=7, choices=range(1, 8),
                   help="ISO weekday for the weekly full (1=Mon .. 7=Sun)")
    r.add_argument("--force-full", action="store_true")
    r.add_argument("--no-prune", action="store_true")

    pr = sub.add_parser("prune", help="apply retention only")
    pr.add_argument("--keep-full", type=int, default=5)
    pr.add_argument("--dry-run", action="store_true")

    rs = sub.add_parser("restore", help="rebuild a store from a full [+ diff]")
    rs.add_argument("full", type=Path)
    rs.add_argument("diff", type=Path, nargs="?")
    rs.add_argument("--out", type=Path, required=True)

    rc = sub.add_parser("restore-check",
                        help="restore the newest full+diff into a scratch file and check it")
    rc.add_argument("--scratch", type=Path, required=True)
    rc.add_argument("--keep", action="store_true", help="keep the restored file")

    v = sub.add_parser("verify", help="re-count a snapshot's files against its manifest")
    v.add_argument("snapshot", type=Path)

    a = p.parse_args(argv)
    try:
        if a.cmd == "run":
            run_backup(db_path=a.db, root=a.root, upload_dir=a.uploads,
                       tokens_file=a.tokens, full_weekday=a.full_weekday,
                       force_full=a.force_full, memory_limit=a.memory_limit,
                       threads=a.threads)
            if not a.no_prune:
                prune(a.root, a.keep_full)
            snaps, _ = list_snapshots(a.root)
            logger.info("snapshots kept: %d (%d full)", len(snaps),
                        sum(s.is_full for s in snaps))
        elif a.cmd == "prune":
            prune(a.root, a.keep_full, dry_run=a.dry_run)
        elif a.cmd == "restore":
            counts = restore(a.full, a.diff, a.out, memory_limit=a.memory_limit,
                             threads=a.threads)
            print(json.dumps(counts, indent=2))
        elif a.cmd == "restore-check":
            full, diff = latest_restore_set(a.root)
            a.scratch.mkdir(parents=True, exist_ok=True)
            out = a.scratch / f"restore-check-{full.stamp}.duckdb"
            logger.info("restoring %s%s into %s", full.stamp,
                        f" + {diff.stamp}" if diff else "", out)
            try:
                counts = restore(full.path, diff.path if diff else None, out,
                                 memory_limit=a.memory_limit, threads=a.threads)
                print(json.dumps(counts, indent=2))
            finally:
                if not a.keep:
                    out.unlink(missing_ok=True)
                    Path(f"{out}.wal").unlink(missing_ok=True)
                    shutil.rmtree(a.scratch / f".{out.name}.tmp", ignore_errors=True)
        elif a.cmd == "verify":
            print(json.dumps(verify(a.snapshot), indent=2))
    except Exception:
        logger.exception("%s failed", a.cmd)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
