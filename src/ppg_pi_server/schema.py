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
            uploaded_at       DOUBLE
        )
        """
    )
