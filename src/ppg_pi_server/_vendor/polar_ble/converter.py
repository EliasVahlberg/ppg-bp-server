"""Offline converter: raw ROP session directory → DuckDB.

The streaming daemon writes per-sensor binary ROP files plus a manifest
and event logs (segments, notes) at recording time. This module reads
those artefacts and produces a DuckDB matching the schema used by
:class:`polar_ble.storage.SensorDB`, plus a couple of additional
columns (``session_uuid``, ``epoch_offset_ns`` on ``sessions``;
``local_segment_id`` on ``segments``) and a new ``notes`` table.

Design properties:

* **Pure offline.** No threads, no callbacks, no async. Fail loudly
  if anything goes wrong; the caller can re-run.
* **Idempotent.** Re-converting the same session deletes prior rows
  for that ``session_uuid`` and re-inserts. Safe to run repeatedly.
* **Append-friendly.** ``--append`` ingests into an existing DuckDB,
  enabling a multi-session batch DB. Idempotency still applies per
  ``session_uuid``.
* **Partial-write tolerant.** ROP files truncated by a SIGKILL'd
  daemon are read up to the last whole record (handled by
  :class:`polar_ble.rop_format.RopReader`).
* **Both notes formats.** Reads ``notes.jsonl`` if present,
  otherwise falls back to ``notes.csv`` for compatibility with
  ``scripts/note.py``.

See ``docs/design/raw_rop_storage.md``.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import duckdb
import pandas as pd

from . import rop_format as rf
from .rop_format import (
    RECORD_SIZE,
    RopReader,
    SensorType,
    UNPACKER,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session artefact loaders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentEvent:
    """One row from segments.jsonl, with whichever fields are present."""

    event: str               # "open" or "close"
    segment_id: int          # local id from the recorder
    ts: float                # POSIX timestamp
    reason: Optional[str] = None
    device: Optional[str] = None


@dataclass(frozen=True)
class Note:
    """One user note: a wall-clock timestamp plus free-form text."""

    ts: float                # POSIX timestamp
    text: str


@dataclass(frozen=True)
class Manifest:
    """Parsed manifest.json for one session."""

    session_uuid: str
    started_at: float
    ended_at: Optional[float]
    device_name: str
    device_address: Optional[str]
    settings: Dict[str, Any]
    epoch_offset_ns: int
    rotation_period_minutes: int
    rop_files: List[str]


def read_manifest(session_dir: Path) -> Manifest:
    """Load and validate manifest.json from a session directory."""
    path = session_dir / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"No manifest.json in {session_dir} — is this a ROP session?"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = ("session_uuid", "started_at", "settings")
    for k in required:
        if k not in raw:
            raise ValueError(
                f"manifest.json is missing required key '{k}'"
            )
    return Manifest(
        session_uuid=str(raw["session_uuid"]),
        started_at=float(raw["started_at"]),
        ended_at=(float(raw["ended_at"]) if raw.get("ended_at") else None),
        device_name=str(raw.get("device_name", "")),
        device_address=raw.get("device_address"),
        settings=dict(raw.get("settings") or {}),
        epoch_offset_ns=int(raw.get("epoch_offset_ns", 0)),
        rotation_period_minutes=int(raw.get("rotation_period_minutes", 15)),
        rop_files=list(raw.get("rop_files") or []),
    )


def read_segments(session_dir: Path) -> List[SegmentEvent]:
    """Parse segments.jsonl. Returns events in file order.

    Lines are read individually so a partially-written trailing line
    is tolerated (just dropped).
    """
    path = session_dir / "segments.jsonl"
    if not path.is_file():
        return []
    out: List[SegmentEvent] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Likely the partial trailing line from a daemon crash.
                logger.warning("skipping malformed segments line: %r", line)
                continue
            try:
                out.append(SegmentEvent(
                    event=str(obj["event"]),
                    segment_id=int(obj["segment_id"]),
                    ts=float(obj["ts"]),
                    reason=obj.get("reason"),
                    device=obj.get("device"),
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("skipping malformed segments entry %r: %s",
                               obj, e)
    return out


def read_notes(session_dir: Path) -> List[Note]:
    """Read notes.jsonl if present, else notes.csv. Returns [] if neither."""
    jsonl = session_dir / "notes.jsonl"
    if jsonl.is_file():
        return _read_notes_jsonl(jsonl)
    csv_path = session_dir / "notes.csv"
    if csv_path.is_file():
        return _read_notes_csv(csv_path)
    return []


def _read_notes_jsonl(path: Path) -> List[Note]:
    out: List[Note] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed notes.jsonl line: %r", line)
                continue
            try:
                out.append(Note(
                    ts=_parse_iso_or_float(obj["ts"]),
                    text=str(obj.get("note") or obj.get("text") or ""),
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("skipping malformed notes entry %r: %s",
                               obj, e)
    return out


def _read_notes_csv(path: Path) -> List[Note]:
    out: List[Note] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts_raw = row.get("timestamp_utc") or row.get("ts") or ""
            text = row.get("note") or row.get("text") or ""
            try:
                out.append(Note(ts=_parse_iso_or_float(ts_raw), text=text))
            except ValueError as e:
                logger.warning("skipping malformed notes.csv row %r: %s",
                               row, e)
    return out


def _parse_iso_or_float(value: Any) -> float:
    """Accept either ISO8601 string or float seconds-since-epoch."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        # Try float first (in case it's a numeric string).
        try:
            return float(s)
        except ValueError:
            pass
        # Then ISO 8601 with Z suffix.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s).timestamp()
    raise ValueError(f"unparseable timestamp value: {value!r}")


# ---------------------------------------------------------------------------
# DuckDB schema
# ---------------------------------------------------------------------------


def init_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create all tables, idempotent. Mirrors SensorDB._init_tables but
    adds ROP-specific columns and a notes table."""
    con.execute("CREATE SEQUENCE IF NOT EXISTS sessions_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY DEFAULT nextval('sessions_seq'),
            session_uuid VARCHAR UNIQUE,
            start_time DOUBLE,
            end_time DOUBLE,
            device_name VARCHAR,
            device_address VARCHAR,
            settings VARCHAR,
            epoch_offset_ns BIGINT,
            rotation_period_minutes INTEGER
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS segments_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY DEFAULT nextval('segments_seq'),
            session_id INTEGER,
            local_segment_id INTEGER,
            connect_time DOUBLE,
            disconnect_time DOUBLE,
            reason VARCHAR
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS segments_session_idx
        ON segments (session_id)
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ppg (
            session_id INTEGER, segment_id INTEGER, timestamp_ns BIGINT,
            ppg0 INTEGER, ppg1 INTEGER, ppg2 INTEGER, ambient INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS acc (
            session_id INTEGER, segment_id INTEGER, timestamp_ns BIGINT,
            x INTEGER, y INTEGER, z INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gyro (
            session_id INTEGER, segment_id INTEGER, timestamp_ns BIGINT,
            x DOUBLE, y DOUBLE, z DOUBLE
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mag (
            session_id INTEGER, segment_id INTEGER, timestamp_ns BIGINT,
            x DOUBLE, y DOUBLE, z DOUBLE, calibration INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ppi (
            session_id INTEGER, segment_id INTEGER, timestamp_ns BIGINT,
            hr INTEGER, pp_interval_ms INTEGER, pp_error_estimate_ms INTEGER,
            blocker BOOLEAN, skin_contact BOOLEAN, skin_contact_supported BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            session_id INTEGER, ts DOUBLE, note VARCHAR
        )
    """)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


@dataclass
class ConversionStats:
    """What the conversion did. Returned to the caller for reporting."""

    session_uuid: str
    db_session_id: int
    samples_per_sensor: Dict[str, int]
    segments: int
    notes: int
    rop_files_processed: int
    replaced_existing: bool


def convert_session(
    session_dir: Path,
    output_path: Optional[Path] = None,
    *,
    append: bool = False,
) -> ConversionStats:
    """Convert a session directory to DuckDB.

    Args:
        session_dir: directory containing manifest.json, raw/*.rop,
            segments.jsonl, notes.{jsonl,csv}.
        output_path: destination DuckDB. If None, defaults to
            ``<session_dir>/session.duckdb``.
        append: if True, write into an existing DuckDB at
            ``output_path``; otherwise create or replace it.

    Returns:
        :class:`ConversionStats` summarising what was inserted.
    """
    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        raise FileNotFoundError(f"{session_dir} is not a directory")

    if output_path is None:
        output_path = session_dir / "session.duckdb"
    output_path = Path(output_path)

    if not append and output_path.exists():
        # NOTE: append=False does NOT wipe the output. It runs the
        # idempotent same-session_uuid replace below, so re-converting
        # one session is a no-op and other sessions already in the file
        # are left untouched. For a guaranteed-empty file, delete it
        # first. (In the default per-session session.duckdb workflow the
        # file is unique to this session, so this is moot.)
        logger.info(
            "Output %s already exists; replacing only this session's rows "
            "(other sessions preserved; use --append for an explicit "
            "multi-session DB).",
            output_path,
        )

    manifest = read_manifest(session_dir)
    segments = read_segments(session_dir)
    notes = read_notes(session_dir)
    rop_files = sorted(_resolve_rop_files(session_dir, manifest))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(output_path))
    try:
        init_tables(con)
        replaced = _replace_existing_session(con, manifest.session_uuid)
        db_session_id = _insert_session(con, manifest)
        seg_map = _insert_segments(con, segments, db_session_id)
        sample_counts = _insert_rop_files(
            con, rop_files, db_session_id, seg_map
        )
        _insert_notes(con, notes, db_session_id)
        # Mark session ended_at if the manifest says so.
        if manifest.ended_at is not None:
            con.execute(
                "UPDATE sessions SET end_time = ? WHERE id = ?",
                [manifest.ended_at, db_session_id],
            )
    finally:
        con.close()

    return ConversionStats(
        session_uuid=manifest.session_uuid,
        db_session_id=db_session_id,
        samples_per_sensor=sample_counts,
        segments=len(seg_map),
        notes=len(notes),
        rop_files_processed=len(rop_files),
        replaced_existing=replaced,
    )


def convert_single_rop(
    rop_path: Path,
    output_path: Path,
    *,
    db_session_id: int,
) -> int:
    """Ingest one .rop file into an already-initialised DuckDB.

    The caller has already created the session row (and segments rows
    if applicable) and supplies ``db_session_id``. Used by the
    ``polar-cli convert <single.rop>`` form.

    Returns the number of records inserted.
    """
    rop_path = Path(rop_path)
    output_path = Path(output_path)
    if not rop_path.is_file():
        raise FileNotFoundError(rop_path)
    if not output_path.is_file():
        raise FileNotFoundError(
            f"{output_path} does not exist; run convert_session first"
        )
    con = duckdb.connect(str(output_path))
    try:
        init_tables(con)
        # No segment map for single-file mode; the segment_id stays
        # whatever was in the ROP record. The caller is responsible
        # for ensuring those IDs exist in the segments table.
        identity_map: Dict[int, int] = {}
        counts = _insert_one_rop(con, rop_path, db_session_id, identity_map,
                                 use_identity_segments=True)
    finally:
        con.close()
    return sum(counts.values())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_rop_files(session_dir: Path, manifest: Manifest) -> List[Path]:
    """Trust the manifest if it lists files, else glob raw/*.rop."""
    if manifest.rop_files:
        return [session_dir / p for p in manifest.rop_files
                if (session_dir / p).is_file()]
    raw = session_dir / "raw"
    if not raw.is_dir():
        return []
    return sorted(raw.glob("*.rop"))


def _replace_existing_session(con: duckdb.DuckDBPyConnection,
                              session_uuid: str) -> bool:
    """If a session with this UUID exists, delete it and all child rows."""
    row = con.execute(
        "SELECT id FROM sessions WHERE session_uuid = ?",
        [session_uuid],
    ).fetchone()
    if row is None:
        return False
    sid = row[0]
    for tbl in ("ppg", "acc", "gyro", "mag", "ppi", "notes"):
        con.execute(f"DELETE FROM {tbl} WHERE session_id = ?", [sid])
    con.execute("DELETE FROM segments WHERE session_id = ?", [sid])
    con.execute("DELETE FROM sessions WHERE id = ?", [sid])
    logger.info("Replaced existing session %s (db id %d)", session_uuid, sid)
    return True


def _insert_session(con: duckdb.DuckDBPyConnection,
                    manifest: Manifest) -> int:
    con.execute(
        "INSERT INTO sessions "
        "(session_uuid, start_time, end_time, device_name, "
        " device_address, settings, epoch_offset_ns, rotation_period_minutes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            manifest.session_uuid,
            manifest.started_at,
            manifest.ended_at,
            manifest.device_name,
            manifest.device_address,
            json.dumps(manifest.settings, separators=(",", ":")),
            manifest.epoch_offset_ns,
            manifest.rotation_period_minutes,
        ],
    )
    sid = con.execute(
        "SELECT id FROM sessions WHERE session_uuid = ?",
        [manifest.session_uuid],
    ).fetchone()[0]
    return int(sid)


def _insert_segments(con: duckdb.DuckDBPyConnection,
                     events: Iterable[SegmentEvent],
                     db_session_id: int) -> Dict[int, int]:
    """Insert one segments row per (open, close?) pair. Returns a
    mapping from local_segment_id to the inserted row's PK."""
    by_id: Dict[int, Dict[str, Any]] = {}
    for ev in events:
        slot = by_id.setdefault(ev.segment_id, {})
        if ev.event == "open":
            slot["connect_time"] = ev.ts
        elif ev.event == "close":
            slot["disconnect_time"] = ev.ts
            slot["reason"] = ev.reason

    seg_map: Dict[int, int] = {}
    for local_id in sorted(by_id):
        slot = by_id[local_id]
        con.execute(
            "INSERT INTO segments "
            "(session_id, local_segment_id, connect_time, "
            " disconnect_time, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                db_session_id,
                local_id,
                slot.get("connect_time"),
                slot.get("disconnect_time"),
                slot.get("reason"),
            ],
        )
        # We just inserted; fetch the PK.
        new_id = con.execute(
            "SELECT id FROM segments "
            "WHERE session_id = ? AND local_segment_id = ?",
            [db_session_id, local_id],
        ).fetchone()[0]
        seg_map[local_id] = int(new_id)
    return seg_map


def _insert_rop_files(con: duckdb.DuckDBPyConnection,
                      rop_files: Iterable[Path],
                      db_session_id: int,
                      seg_map: Dict[int, int]) -> Dict[str, int]:
    counts = {"ppg": 0, "acc": 0, "gyro": 0, "mag": 0, "ppi": 0}
    for path in rop_files:
        per_file = _insert_one_rop(con, path, db_session_id, seg_map,
                                   use_identity_segments=False)
        for k, v in per_file.items():
            counts[k] += v
    return counts


# Per-sensor INSERT statements. Pre-built so we don't pay the SQL
# parsing cost per-record. Used as the single-row fallback path; the
# bulk path uses con.append(table_name, dataframe) instead and is
# orders of magnitude faster.
_INSERT_SQL = {
    SensorType.PPG: (
        "INSERT INTO ppg "
        "(session_id, segment_id, timestamp_ns, ppg0, ppg1, ppg2, ambient) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    ),
    SensorType.ACC: (
        "INSERT INTO acc "
        "(session_id, segment_id, timestamp_ns, x, y, z) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    ),
    SensorType.GYRO: (
        "INSERT INTO gyro "
        "(session_id, segment_id, timestamp_ns, x, y, z) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    ),
    SensorType.MAG: (
        "INSERT INTO mag "
        "(session_id, segment_id, timestamp_ns, x, y, z, calibration) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    ),
    SensorType.PPI: (
        "INSERT INTO ppi "
        "(session_id, segment_id, timestamp_ns, "
        " hr, pp_interval_ms, pp_error_estimate_ms, "
        " blocker, skin_contact, skin_contact_supported) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
}


# Column names per sensor for the bulk-append path. Order must match
# the table column order in :func:`init_tables` (we use by_name=True
# below to make the match explicit anyway).
_COLUMNS = {
    SensorType.PPG: ("session_id", "segment_id", "timestamp_ns",
                     "ppg0", "ppg1", "ppg2", "ambient"),
    SensorType.ACC: ("session_id", "segment_id", "timestamp_ns",
                     "x", "y", "z"),
    SensorType.GYRO: ("session_id", "segment_id", "timestamp_ns",
                      "x", "y", "z"),
    SensorType.MAG: ("session_id", "segment_id", "timestamp_ns",
                     "x", "y", "z", "calibration"),
    SensorType.PPI: ("session_id", "segment_id", "timestamp_ns",
                     "hr", "pp_interval_ms", "pp_error_estimate_ms",
                     "blocker", "skin_contact", "skin_contact_supported"),
}


# Insert chunk size. The converter reads ROP files via RopReader and
# accumulates rows up to this many before flushing via con.append().
# Bounds peak memory regardless of session length: a 4-hour
# calibration at ~1 kHz across all sensors is ~14M rows, but the
# converter never holds more than CHUNK_SIZE * row_width in Python
# memory at once.
CHUNK_SIZE = 50_000


def _flush_chunk(con: duckdb.DuckDBPyConnection, sensor: SensorType,
                 chunk: List[tuple]) -> None:
    """Flush ``chunk`` into the sensor's table via DuckDB's bulk
    appender. ``chunk`` is a list of positional tuples whose ordering
    matches ``_COLUMNS[sensor]``.

    Uses :func:`pandas.DataFrame` as the intermediate because the
    installed DuckDB build's ``con.append`` accepts a pandas
    DataFrame. This bypasses the per-row parameter binding that
    makes ``executemany`` painfully slow for multi-million-row
    sessions.
    """
    cols = _COLUMNS[sensor]
    df = pd.DataFrame(chunk, columns=list(cols))
    # ``segment_id`` is the only column that can be NULL (samples
    # produced before any segment was opened). Use pandas' nullable
    # Int64 dtype so the column doesn't get silently coerced to
    # float64 by NaN, which DuckDB would reject for an INTEGER
    # column.
    df["segment_id"] = df["segment_id"].astype(pd.Int64Dtype())
    table = sensor.name.lower()
    con.append(table, df, by_name=True)


def _insert_one_rop(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    db_session_id: int,
    seg_map: Dict[int, int],
    *,
    use_identity_segments: bool,
) -> Dict[str, int]:
    """Read one ROP file and bulk-insert its records in chunks.

    If ``use_identity_segments`` is True the record's ``segment_id``
    is passed through unchanged (single-file ingest mode); otherwise
    it is translated through ``seg_map`` (full-session mode).
    """
    counts = {"ppg": 0, "acc": 0, "gyro": 0, "mag": 0, "ppi": 0}
    with RopReader(path) as r:
        sensor = r.header.sensor
        unpack = UNPACKER[sensor]
        chunk: List[tuple] = []
        total = 0
        for raw in r:
            rec = unpack(raw)
            seg = (rec.segment_id if use_identity_segments
                   else seg_map.get(rec.segment_id))
            chunk.append(_record_to_row(sensor, db_session_id, seg, rec))
            if len(chunk) >= CHUNK_SIZE:
                _flush_chunk(con, sensor, chunk)
                total += len(chunk)
                chunk = []
        if chunk:
            _flush_chunk(con, sensor, chunk)
            total += len(chunk)
        counts[sensor.name.lower()] = total
    return counts


def _record_to_row(sensor: SensorType, sid: int, seg: Optional[int],
                   rec: Any) -> tuple:
    """Convert a typed record to a positional tuple matching its INSERT."""
    if sensor == SensorType.PPG:
        return (sid, seg, rec.ts_ns, rec.ppg0, rec.ppg1, rec.ppg2, rec.ambient)
    if sensor == SensorType.ACC:
        return (sid, seg, rec.ts_ns, rec.x, rec.y, rec.z)
    if sensor == SensorType.GYRO:
        return (sid, seg, rec.ts_ns, float(rec.x), float(rec.y), float(rec.z))
    if sensor == SensorType.MAG:
        return (sid, seg, rec.ts_ns,
                float(rec.x), float(rec.y), float(rec.z),
                rec.calibration)
    if sensor == SensorType.PPI:
        return (sid, seg, rec.ts_ns,
                rec.hr, rec.ppi_ms, rec.err_ms,
                bool(rec.blocker), bool(rec.skin_contact),
                bool(rec.sc_supported))
    raise ValueError(f"unknown sensor {sensor}")


def _insert_notes(con: duckdb.DuckDBPyConnection,
                  notes: Iterable[Note], db_session_id: int) -> None:
    rows = [(db_session_id, n.ts, n.text) for n in notes]
    if rows:
        con.executemany(
            "INSERT INTO notes (session_id, ts, note) VALUES (?, ?, ?)",
            rows,
        )
