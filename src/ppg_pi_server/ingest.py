"""Bundle ingest: stage raw ROP session bundles, then convert via the
shared converter into the canonical DuckDB store.

Flow:

1. ``open_session``  — create an ``uploads`` audit row.
2. ``stage_file``    — verify SHA-256, write the file into the session's
   bundle directory, record it in the audit row.
3. ``complete``      — run ``converter.convert_session(bundle_dir)`` into the
   canonical store (append), record conversion stats.

DuckDB connections are short-lived and serialized by a process lock, so the
converter (which opens its own connection) never races us.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import duckdb

from ._corelib import converter
from .schema import init_audit_schema, init_cuff_schema

logger = logging.getLogger("ppg_pi_server.ingest")

# Allowed bundle filenames: the fixed metadata files plus per-sensor *.rop.
_ALLOWED_RE = re.compile(
    r"^(manifest\.json|segments\.jsonl|notes\.jsonl|notes\.csv"
    r"|[A-Za-z0-9][A-Za-z0-9._-]*\.rop)$"
)


class IngestError(Exception):
    """Raised when a bundle file or conversion cannot be accepted."""


def valid_filename(name: str) -> bool:
    return "/" not in name and ".." not in name and bool(_ALLOWED_RE.match(name))


@dataclass
class CompleteResult:
    phone_session_uuid: str
    db_session_id: int
    samples_per_sensor: dict
    segments: int
    notes: int
    rop_files: int


class Ingestor:
    """Connection-per-operation DuckDB helper, serialized by a lock.

    No persistent connection is held, so ``converter.convert_session`` (which
    opens its own connection to the same file) never collides with us.
    """

    def __init__(self, db_path: Path, upload_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.upload_dir = Path(upload_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            init_audit_schema(con)
            init_cuff_schema(con)

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        # DuckDB allows only one writer at a time. If the dashboard or analysis
        # process holds the lock (e.g. during /refresh), retry with backoff
        # rather than crashing the request. Total wait: ~45s (enough for a full
        # process_canonical_store run which takes ~28s).
        import time as _time
        last_exc: Exception | None = None
        for attempt in range(12):
            try:
                con = duckdb.connect(str(self.db_path))
                break
            except duckdb.IOException as exc:
                last_exc = exc
                wait = min(2.0 * (attempt + 1), 5.0)
                if attempt < 11:
                    logger.warning("DB lock contention (attempt %d/12, retry in %.1fs): %s",
                                   attempt + 1, wait, exc)
                    _time.sleep(wait)
        else:
            logger.error("DB lock acquisition failed after 12 attempts: %s", last_exc)
            raise last_exc  # type: ignore[misc]
        try:
            yield con
        finally:
            con.close()

    def close(self) -> None:
        """No persistent connection; kept for lifespan symmetry."""

    def _bundle_dir(self, phone_session_uuid: str) -> Path:
        return self.upload_dir / phone_session_uuid

    # ----------------------------------------------------------- sessions

    def open_session(
        self, *, phone_session_uuid: str, uploader_phone_id: str,
        device_name: str | None,
    ) -> bool:
        """Create the audit row. Returns True if it already existed."""
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM uploads WHERE phone_session_uuid = ?",
                [phone_session_uuid],
            ).fetchone()
            if row is not None:
                return True
            con.execute(
                "INSERT INTO uploads (phone_session_uuid, uploader_phone_id, "
                "device_name, opened_at, completed_at, status, files_json, "
                "convert_stats_json) VALUES (?, ?, ?, ?, NULL, 'open', '{}', NULL)",
                [phone_session_uuid, uploader_phone_id, device_name, time.time()],
            )
            return False

    def get_session(self, phone_session_uuid: str) -> dict | None:
        with self._lock, self._connect() as con:
            cur = con.execute(
                "SELECT * FROM uploads WHERE phone_session_uuid = ?",
                [phone_session_uuid],
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def list_sessions(self, *, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as con:
            cur = con.execute(
                "SELECT phone_session_uuid, uploader_phone_id, device_name, "
                "opened_at, completed_at, status, files_json, convert_stats_json "
                "FROM uploads ORDER BY opened_at DESC NULLS LAST LIMIT ?",
                [limit],
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ----------------------------------------------------------- staging

    def stage_file(
        self, *, phone_session_uuid: str, filename: str, content: bytes,
        expected_sha256: str | None,
    ) -> tuple[str, int]:
        """Verify + write one bundle file. Returns (sha256, bytes)."""
        if not valid_filename(filename):
            raise IngestError(f"Illegal bundle filename: {filename!r}")
        actual = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            logger.error("SHA-256 mismatch: uuid=%s file=%s expected=%s actual=%s",
                         phone_session_uuid[:8], filename, expected_sha256[:12], actual[:12])
            raise IngestError(
                f"SHA-256 mismatch for {filename}: expected {expected_sha256}, "
                f"got {actual}"
            )
        bdir = self._bundle_dir(phone_session_uuid)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / filename).write_bytes(content)
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT files_json FROM uploads WHERE phone_session_uuid = ?",
                [phone_session_uuid],
            ).fetchone()
            files = json.loads(row[0]) if row and row[0] else {}
            files[filename] = {"sha256": actual, "bytes": len(content)}
            con.execute(
                "UPDATE uploads SET files_json = ? WHERE phone_session_uuid = ?",
                [json.dumps(files, sort_keys=True), phone_session_uuid],
            )
        return actual, len(content)

    # ----------------------------------------------------------- cuff

    def ingest_cuff_readings(
        self, *, readings: list[dict], uploader_phone_id: str
    ) -> tuple[int, int, int]:
        """Upsert standalone cuff readings, deduped by ``reading_id``.

        Idempotent: re-uploading the phone's whole local store inserts only
        rows not already present. Returns ``(received, inserted, total)``.
        """
        if not readings:
            with self._lock, self._connect() as con:
                total = con.execute("SELECT count(*) FROM cuff_readings").fetchone()[0]
            return 0, 0, int(total)
        now = time.time()
        with self._lock, self._connect() as con:
            before = con.execute("SELECT count(*) FROM cuff_readings").fetchone()[0]
            for r in readings:
                con.execute(
                    "INSERT INTO cuff_readings (reading_id, taken_at, sys, dia, "
                    "pulse, ihb, mov, device, uploader_phone_id, uploaded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (reading_id) DO NOTHING",
                    [
                        r["id"], r["ts"], r["sys"], r["dia"], r["pulse"],
                        bool(r.get("ihb", False)), bool(r.get("mov", False)),
                        r.get("device"), uploader_phone_id, now,
                    ],
                )
            after = con.execute("SELECT count(*) FROM cuff_readings").fetchone()[0]
        return len(readings), int(after - before), int(after)

    def count_cuff_readings(self) -> int:
        with self._lock, self._connect() as con:
            return int(con.execute("SELECT count(*) FROM cuff_readings").fetchone()[0])

    # ----------------------------------------------------------- convert

    def complete(self, *, phone_session_uuid: str) -> CompleteResult:
        """Convert the staged bundle into the canonical store."""
        bdir = self._bundle_dir(phone_session_uuid)
        if not (bdir / "manifest.json").is_file():
            raise IngestError("No manifest.json staged for this session")
        logger.info("converting bundle: uuid=%s dir=%s", phone_session_uuid[:8], bdir)
        t0 = time.time()
        with self._lock:
            try:
                stats = converter.convert_session(bdir, self.db_path, append=True)
            except Exception as exc:  # noqa: BLE001 - surface as ingest failure
                logger.error("conversion FAILED: uuid=%s elapsed=%.1fs err=%s",
                             phone_session_uuid[:8], time.time() - t0, exc,
                             exc_info=True)
                with self._connect() as con:
                    con.execute(
                        "UPDATE uploads SET status = 'error' "
                        "WHERE phone_session_uuid = ?",
                        [phone_session_uuid],
                    )
                raise IngestError(f"Conversion failed: {exc}") from exc
            elapsed = time.time() - t0
            logger.info("conversion OK: uuid=%s db_id=%d elapsed=%.1fs "
                        "ppg=%d acc=%d gyro=%d mag=%d ppi=%d segs=%d rops=%d",
                        phone_session_uuid[:8], stats.db_session_id, elapsed,
                        stats.samples_per_sensor.get("ppg", 0),
                        stats.samples_per_sensor.get("acc", 0),
                        stats.samples_per_sensor.get("gyro", 0),
                        stats.samples_per_sensor.get("mag", 0),
                        stats.samples_per_sensor.get("ppi", 0),
                        stats.segments, stats.rop_files_processed)
            stats_json = json.dumps(
                {
                    "samples_per_sensor": stats.samples_per_sensor,
                    "segments": stats.segments,
                    "notes": stats.notes,
                    "rop_files": stats.rop_files_processed,
                    "db_session_id": stats.db_session_id,
                },
                sort_keys=True,
            )
            with self._connect() as con:
                con.execute(
                    "UPDATE uploads SET status = 'complete', completed_at = ?, "
                    "convert_stats_json = ? WHERE phone_session_uuid = ?",
                    [time.time(), stats_json, phone_session_uuid],
                )
        return CompleteResult(
            phone_session_uuid=phone_session_uuid,
            db_session_id=stats.db_session_id,
            samples_per_sensor=dict(stats.samples_per_sensor),
            segments=stats.segments,
            notes=stats.notes,
            rop_files=stats.rop_files_processed,
        )
