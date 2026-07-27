"""Server-owned audit schema.

The canonical *data* tables (sessions, segments, ppg, acc, gyro, mag, ppi,
notes) are created by the shared converter (``polar_ble.converter.init_tables``)
when a bundle is converted. This module owns only the server's upload
bookkeeping table, which the converter knows nothing about.
"""

from __future__ import annotations

import duckdb


def init_audit_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the ``uploads`` audit table (idempotent)."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            phone_session_uuid VARCHAR PRIMARY KEY,
            uploader_phone_id  VARCHAR,
            device_name        VARCHAR,
            opened_at          DOUBLE,
            completed_at       DOUBLE,
            status             VARCHAR,           -- 'open' | 'complete' | 'error'
            files_json         VARCHAR,           -- {filename: {sha256, bytes}}
            convert_stats_json VARCHAR
        )
        """
    )


def init_cuff_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the ``cuff_readings`` table (idempotent).

    Standalone oscillometric reference readings synced from the phone (Omron
    Evolv). Not part of the ROP session bundle: the cuff exposes a rolling
    buffer, so uploads are deduped by ``reading_id`` (taken-at|sys|dia|pulse).
    Lives in the canonical store so analysis can pair them with PPG by time
    (cf. desktop ``build_calibration_pairs.py``). The converter is unaware of
    this table; the server owns it.
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cuff_readings (
            reading_id        VARCHAR PRIMARY KEY,  -- taken_at|sys|dia|pulse
            taken_at          VARCHAR,              -- ISO-8601 local wall time
            sys               INTEGER,
            dia               INTEGER,
            pulse             INTEGER,
            ihb               BOOLEAN,              -- irregular heartbeat flag
            mov               BOOLEAN,              -- body-movement flag
            device            VARCHAR,              -- cuff BLE address/name
            uploader_phone_id VARCHAR,
            uploaded_at       DOUBLE,

            -- Clock provenance, measured by the phone on every cuff read
            -- (ppg-bp-android#9). taken_at above is the cuff's own wall time and
            -- is never rewritten, because the phone's dedup identity is derived
            -- from it. Corrected time is derived at analysis time as
            -- taken_at - clock_offset_s, which is why the offset has to be
            -- stored per reading rather than assumed constant.
            phone_read_at              VARCHAR,   -- ISO-8601, phone clock at read
            clock_offset_s             DOUBLE,    -- cuff minus phone; NULL if unmeasurable
            clock_offset_uncertainty_s DOUBLE,    -- half the BLE read window
            clock_valid                BOOLEAN,   -- False => timestamp not trustworthy
            clock_suspect              BOOLEAN,   -- quarantined on the phone
            slot                       INTEGER    -- ring-buffer slot, quarantine only
        )
        """
    )
    _migrate_cuff_columns(con)


# Columns added after the table first shipped. DuckDB has no "ADD COLUMN IF NOT
# EXISTS", and this project has no migration framework, so reconcile by
# inspecting the catalogue. Additive only: never drops or retypes a column, so a
# database that predates any of these is upgraded in place without touching rows.
_CUFF_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("phone_read_at", "VARCHAR"),
    ("clock_offset_s", "DOUBLE"),
    ("clock_offset_uncertainty_s", "DOUBLE"),
    ("clock_valid", "BOOLEAN"),
    ("clock_suspect", "BOOLEAN"),
    ("slot", "INTEGER"),
)


def _migrate_cuff_columns(con: duckdb.DuckDBPyConnection) -> None:
    existing = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'cuff_readings'"
        ).fetchall()
    }
    for name, sql_type in _CUFF_ADDED_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE cuff_readings ADD COLUMN {name} {sql_type}")
