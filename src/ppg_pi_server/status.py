"""Collection status: is the data actually arriving?

This is deliberately not analysis. The question it answers is operational --
did today's measurement land, is the cuff about to overwrite unsynced readings,
is any clock untrustworthy, are there calibration pairs yet -- because that is
what determines whether there will be anything to analyse later.

Two layers, split so the interesting half is testable without a database:

* :func:`collect` runs read-only SQL and returns plain numbers.
* :func:`assess` turns those numbers into warnings and is pure.

Everything is grouped by *subject*, identified by the uploading phone
(``uploader_phone_id`` / the token's ``phone_id``). A device is not a person, so
this is a proxy rather than a real subject key, but it is the only discriminator
the schema has -- and showing it everywhere is what stops one person's readings
being averaged into another's, or a cuff reading being paired against somebody
else's PPG.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import duckdb

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Cuff ring buffer size. Readings past this are overwritten with no error
#: (ppg-bp-android#10), so the only defence is transferring often enough.
CUFF_RING_SLOTS = 100

#: Warn when the estimated unsynced readings pass this share of the ring.
CUFF_FILL_WARN = 0.70

#: A recording gap this long means collection has probably stopped.
RECORDING_STALE_S = 48 * 3600

#: Matches the app's red threshold for time since the last cuff transfer.
CUFF_STALE_S = 14 * 24 * 3600

#: Below this share of usable PPG minutes, the sensor placement or wear time is
#: suspect rather than the analysis being unlucky.
GOOD_MINUTE_SHARE_WARN = 0.70

#: Tolerance when deciding whether a cuff reading falls inside a recording.
#: The cuff timestamp has minute resolution and its deflation takes 30-60 s, so
#: a reading started just before the recording still belongs to it.
PAIR_WINDOW_S = 120.0

#: Protocol target for a usable calibration set.
PAIR_TARGET = 20


@dataclass
class Subject:
    """Per-subject collection state."""

    subject_id: str
    sessions: int = 0
    recorded_hours: float = 0.0
    last_session_at: float | None = None
    cuff_readings: int = 0
    last_cuff_taken_at: str | None = None
    last_cuff_transfer_at: float | None = None
    cuff_per_day: float | None = None
    pairs: int = 0

    def as_dict(self, now: float) -> dict[str, Any]:
        d = {
            "subject_id": self.subject_id,
            "sessions": self.sessions,
            "recorded_hours": round(self.recorded_hours, 2),
            "last_session_at": self.last_session_at,
            "last_session_age_s": _age(self.last_session_at, now),
            "cuff_readings": self.cuff_readings,
            "last_cuff_taken_at": self.last_cuff_taken_at,
            "last_cuff_transfer_at": self.last_cuff_transfer_at,
            "last_cuff_transfer_age_s": _age(self.last_cuff_transfer_at, now),
            "cuff_per_day": self.cuff_per_day,
            "pairs": self.pairs,
        }
        d["estimated_unsynced_cuff"] = estimated_unsynced_cuff(
            self.cuff_per_day, d["last_cuff_transfer_age_s"]
        )
        return d


def _age(then: float | None, now: float) -> float | None:
    return None if then is None else max(0.0, now - then)


def estimated_unsynced_cuff(per_day: float | None, transfer_age_s: float | None) -> float | None:
    """Readings the cuff has probably taken since the last transfer.

    An estimate by necessity: the phone knows the cuff's unread count at read
    time, but that number is not uploaded, so the server can only extrapolate
    from the observed rate. Deliberately reported as an estimate rather than
    presented as the true buffer state.
    """
    if per_day is None or transfer_age_s is None:
        return None
    return round(per_day * transfer_age_s / 86400.0, 1)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def local_timezone(con: duckdb.DuckDBPyConnection) -> str:
    """The zone the cuff's wall-clock timestamps should be interpreted in.

    ``cuff_readings.taken_at`` is the cuff's own *local wall time*, while
    ``sessions.start_time`` is epoch seconds. Comparing them requires knowing the
    zone: DuckDB's ``epoch()`` treats a naive timestamp as UTC, which silently
    shifts every cuff reading by the UTC offset (2 h in Swedish summer, 1 h in
    winter) and makes calibration pairs vanish rather than fail loudly.

    Defaults to the server's own zone, which is right as long as the phone and
    the server are in the same one. Override with
    ``PPG_PI_SERVER_LOCAL_TIMEZONE`` if they are not.
    """
    return str(con.execute("SELECT current_setting('TimeZone')").fetchone()[0])


def collect(
    con: duckdb.DuckDBPyConnection,
    *,
    now: float | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Read the store and return the status payload. Read-only."""
    now = time.time() if now is None else now
    tz = timezone or local_timezone(con)
    tables = {
        r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    }

    def count(table: str, where: str = "") -> int:
        if table not in tables:
            return 0
        return int(con.execute(f'SELECT count(*) FROM "{table}" {where}').fetchone()[0])

    subjects: dict[str, Subject] = {}

    def subject(sid: str | None) -> Subject:
        key = sid or "unknown"
        return subjects.setdefault(key, Subject(subject_id=key))

    # -- recordings. sessions has no uploader column, so attribution comes from
    # -- the uploads audit row, which does.
    if {"sessions", "uploads"} <= tables:
        rows = con.execute(
            """
            SELECT coalesce(u.uploader_phone_id, 'unknown') AS sid,
                   count(*) AS n,
                   sum(coalesce(s.end_time, s.start_time) - s.start_time) AS secs,
                   max(s.start_time) AS last_start
            FROM sessions s
            LEFT JOIN uploads u ON u.phone_session_uuid = s.session_uuid
            GROUP BY 1
            """
        ).fetchall()
        for sid, n, secs, last_start in rows:
            s = subject(sid)
            s.sessions = int(n)
            s.recorded_hours = float(secs or 0.0) / 3600.0
            s.last_session_at = float(last_start) if last_start is not None else None

    # -- cuff readings, with the observed rate over the readings actually held.
    if "cuff_readings" in tables:
        rows = con.execute(
            """
            SELECT coalesce(uploader_phone_id, 'unknown') AS sid,
                   count(*) AS n,
                   max(taken_at) AS last_taken,
                   max(uploaded_at) AS last_upload,
                   min(epoch(timezone(?, taken_at::TIMESTAMP))) AS first_epoch,
                   max(epoch(timezone(?, taken_at::TIMESTAMP))) AS last_epoch
            FROM cuff_readings
            GROUP BY 1
            """,
            [tz, tz],
        ).fetchall()
        for sid, n, last_taken, last_upload, first_epoch, last_epoch in rows:
            s = subject(sid)
            s.cuff_readings = int(n)
            s.last_cuff_taken_at = last_taken
            # uploaded_at is epoch seconds (DOUBLE), unlike taken_at which is the
            # cuff's own ISO wall time. Different clocks, deliberately kept apart.
            s.last_cuff_transfer_at = float(last_upload) if last_upload is not None else None
            span_days = ((last_epoch or 0) - (first_epoch or 0)) / 86400.0
            if n and span_days > 0.5:
                s.cuff_per_day = round(int(n) / span_days, 2)

    # -- calibration pairs: a cuff reading whose corrected time falls inside a
    # -- recording from the same subject. Corrected time is taken_at minus
    # -- clock_offset_s (the offset is cuff-minus-phone), never a rewritten
    # -- timestamp. Readings with an untrustworthy clock cannot be paired.
    if {"cuff_readings", "sessions", "uploads"} <= tables:
        rows = con.execute(
            f"""
            SELECT coalesce(cr.uploader_phone_id, 'unknown') AS sid, count(*) AS n
            FROM cuff_readings cr
            WHERE coalesce(cr.clock_valid, FALSE)
              AND NOT coalesce(cr.clock_suspect, FALSE)
              AND EXISTS (
                SELECT 1
                FROM sessions s
                LEFT JOIN uploads u ON u.phone_session_uuid = s.session_uuid
                WHERE coalesce(u.uploader_phone_id, 'unknown')
                      = coalesce(cr.uploader_phone_id, 'unknown')
                  AND (epoch(timezone(?, cr.taken_at::TIMESTAMP))
                       - coalesce(cr.clock_offset_s, 0))
                      BETWEEN s.start_time - {PAIR_WINDOW_S}
                          AND coalesce(s.end_time, s.start_time) + {PAIR_WINDOW_S}
              )
            GROUP BY 1
            """,
            [tz],
        ).fetchall()
        for sid, n in rows:
            subject(sid).pairs = int(n)

    clock: dict[str, Any] = {"cuff_total": count("cuff_readings")}
    if "cuff_readings" in tables:
        row = con.execute(
            """
            SELECT sum(CASE WHEN clock_offset_s IS NOT NULL THEN 1 ELSE 0 END),
                   sum(CASE WHEN coalesce(clock_valid, FALSE) THEN 0 ELSE 1 END),
                   sum(CASE WHEN coalesce(clock_suspect, FALSE) THEN 1 ELSE 0 END),
                   max(abs(clock_offset_s))
            FROM cuff_readings
            """
        ).fetchone()
        clock.update(
            with_provenance=int(row[0] or 0),
            not_valid=int(row[1] or 0),
            suspect=int(row[2] or 0),
            max_abs_offset_s=float(row[3]) if row[3] is not None else None,
        )

    quality: dict[str, Any] = {"minutes": 0, "good_minutes": 0}
    if "derived_ppg_minute" in tables:
        row = con.execute(
            "SELECT count(*), sum(CASE WHEN sqi > 0.8 THEN 1 ELSE 0 END) FROM derived_ppg_minute"
        ).fetchone()
        quality = {"minutes": int(row[0] or 0), "good_minutes": int(row[1] or 0)}

    uploads: dict[str, int] = {}
    if "uploads" in tables:
        uploads = {
            str(st): int(n)
            for st, n in con.execute(
                "SELECT status, count(*) FROM uploads GROUP BY 1"
            ).fetchall()
        }

    payload = {
        "generated_at": now,
        "timezone": tz,
        "totals": {
            "sessions": count("sessions"),
            "ppg_samples": count("ppg"),
            "acc_samples": count("acc"),
            "gyro_samples": count("gyro"),
            "cuff_readings": count("cuff_readings"),
            "notes": count("notes"),
        },
        "subjects": [
            s.as_dict(now) for s in sorted(subjects.values(), key=lambda x: x.subject_id)
        ],
        "clock": clock,
        "quality": quality,
        "uploads": uploads,
        "recent_sessions": _recent_sessions(con, tables),
        "recent_cuff": _recent_cuff(con, tables),
        "markers": _markers(con, tables),
    }
    payload["warnings"] = assess(payload)
    return payload


def _recent_sessions(con: duckdb.DuckDBPyConnection, tables: set[str]) -> list[dict]:
    if not {"sessions", "uploads"} <= tables:
        return []
    rows = con.execute(
        """
        SELECT s.id, s.session_uuid, s.start_time, s.end_time, s.device_name,
               coalesce(u.uploader_phone_id, 'unknown') AS subject_id, u.status,
               (SELECT count(*) FROM notes n WHERE n.session_id = s.id) AS notes
        FROM sessions s
        LEFT JOIN uploads u ON u.phone_session_uuid = s.session_uuid
        ORDER BY s.start_time DESC
        LIMIT 15
        """
    ).fetchall()
    return [
        {
            "id": r[0],
            "uuid": (r[1] or "")[:8],
            "start_time": r[2],
            "duration_s": (r[3] - r[2]) if (r[3] and r[2]) else None,
            "device_name": r[4],
            "subject_id": r[5],
            "status": r[6],
            "notes": r[7],
        }
        for r in rows
    ]


def _recent_cuff(con: duckdb.DuckDBPyConnection, tables: set[str]) -> list[dict]:
    if "cuff_readings" not in tables:
        return []
    rows = con.execute(
        """
        SELECT taken_at, sys, dia, pulse, coalesce(uploader_phone_id, 'unknown'),
               clock_offset_s, coalesce(clock_valid, FALSE), coalesce(clock_suspect, FALSE),
               coalesce(ihb, FALSE), coalesce(mov, FALSE)
        FROM cuff_readings ORDER BY taken_at DESC LIMIT 15
        """
    ).fetchall()
    return [
        {
            "taken_at": r[0],
            "sys": r[1],
            "dia": r[2],
            "pulse": r[3],
            "subject_id": r[4],
            "clock_offset_s": r[5],
            "clock_valid": bool(r[6]),
            "clock_suspect": bool(r[7]),
            "irregular": bool(r[8]),
            "movement": bool(r[9]),
        }
        for r in rows
    ]


def _markers(con: duckdb.DuckDBPyConnection, tables: set[str]) -> list[dict]:
    """Calibration session delimiters written by the app (ppg-bp-android)."""
    if "notes" not in tables:
        return []
    rows = con.execute(
        """
        SELECT session_id, ts,
               json_extract_string(note, '$.event'),
               json_extract_string(note, '$.name'),
               json_extract(note, '$.tags')::VARCHAR
        FROM notes
        WHERE json_extract_string(note, '$.event') IN
              ('calibration_start', 'calibration_stop')
        ORDER BY ts DESC LIMIT 20
        """
    ).fetchall()
    return [
        {"session_id": r[0], "ts": r[1], "event": r[2], "name": r[3], "tags": r[4]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Assessment (pure)
# ---------------------------------------------------------------------------


@dataclass
class Warning_:
    level: str  # "error" | "warn" | "info"
    subject_id: str | None
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "subject_id": self.subject_id,
            "message": self.message,
            "detail": self.detail,
        }


def assess(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn collected numbers into ranked warnings. Pure."""
    out: list[Warning_] = []
    now = payload.get("generated_at", 0.0)
    del now  # ages are precomputed; kept explicit so callers cannot pass a stale clock

    for s in payload.get("subjects", []):
        sid = s["subject_id"]

        age = s.get("last_session_age_s")
        if age is None:
            out.append(Warning_("warn", sid, "No recordings yet"))
        elif age > RECORDING_STALE_S:
            out.append(
                Warning_(
                    "error",
                    sid,
                    f"No recording for {_days(age)}",
                    "Collection may have stopped.",
                )
            )

        t_age = s.get("last_cuff_transfer_age_s")
        if t_age is not None and t_age > CUFF_STALE_S:
            out.append(
                Warning_(
                    "error",
                    sid,
                    f"Cuff not transferred for {_days(t_age)}",
                    "The cuff holds 100 readings and then overwrites silently.",
                )
            )
        est = s.get("estimated_unsynced_cuff")
        if est is not None and est >= CUFF_RING_SLOTS:
            out.append(
                Warning_(
                    "error",
                    sid,
                    "Cuff buffer estimated full",
                    f"~{est:g} readings since the last transfer, ring holds "
                    f"{CUFF_RING_SLOTS}. Older readings are already unrecoverable.",
                )
            )
        elif est is not None and est >= CUFF_RING_SLOTS * CUFF_FILL_WARN:
            out.append(
                Warning_(
                    "warn",
                    sid,
                    "Cuff buffer filling",
                    f"~{est:g} of {CUFF_RING_SLOTS} slots estimated used since "
                    "the last transfer.",
                )
            )

        pairs = s.get("pairs", 0)
        if pairs == 0:
            out.append(
                Warning_(
                    "warn",
                    sid,
                    "No calibration pairs",
                    "No cuff reading falls inside a recording, so nothing can be "
                    "calibrated yet.",
                )
            )
        elif pairs < PAIR_TARGET:
            out.append(
                Warning_(
                    "info",
                    sid,
                    f"{pairs} of {PAIR_TARGET} calibration pairs",
                    "Protocol target is at least 20 pairs spanning 30 mmHg.",
                )
            )

    clock = payload.get("clock", {})
    if clock.get("not_valid"):
        out.append(
            Warning_(
                "error",
                None,
                f"{clock['not_valid']} cuff readings have an untrustworthy clock",
                "These cannot be paired with PPG.",
            )
        )
    if clock.get("suspect"):
        out.append(
            Warning_("warn", None, f"{clock['suspect']} cuff readings quarantined on the phone")
        )

    incomplete = sum(n for st, n in payload.get("uploads", {}).items() if st != "complete")
    if incomplete:
        out.append(
            Warning_(
                "error",
                None,
                f"{incomplete} uploads never completed",
                "The bundle reached the server but was not converted.",
            )
        )

    q = payload.get("quality", {})
    if q.get("minutes"):
        share = q.get("good_minutes", 0) / q["minutes"]
        if share < GOOD_MINUTE_SHARE_WARN:
            out.append(
                Warning_(
                    "warn",
                    None,
                    f"Only {share:.0%} of PPG minutes are usable",
                    "Check sensor placement and skin contact.",
                )
            )

    rank = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda w: rank.get(w.level, 9))
    return [w.as_dict() for w in out]


def _days(seconds: float) -> str:
    d = seconds / 86400.0
    if d < 1:
        return f"{seconds / 3600.0:.0f} h"
    return f"{d:.0f} d"
