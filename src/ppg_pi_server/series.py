"""Chart series for the web UI.

Separate from :mod:`status` because the questions differ. Status answers "is the
data arriving"; these series answer "what does it look like". Both are read-only.

Two rules run through all of it:

* Series are grouped by subject and never merged. Averaging one person's blood
  pressure into another's would produce a plausible-looking line that means
  nothing, and a chart is exactly where that mistake stops being visible.
* No PPG-derived blood pressure. The cuff is the only pressure reference here
  (see the project's own principle, and Dias 2024 for why absolute PPG numbers
  are not defensible). PPG contributes heart rate and signal quality only.
"""

from __future__ import annotations

from typing import Any

import duckdb

from .status import local_timezone

#: Cap on returned cuff points. Two months of six-a-day readings is ~360, so this
#: is generous; it exists so a pathological store cannot blow up the payload.
MAX_CUFF_POINTS = 5000

#: Quality is aggregated hourly rather than sent per minute. A month of recording
#: would otherwise be tens of thousands of points that no phone screen can show.
QUALITY_BUCKET = "hour"

#: Half-window for attaching a PPG heart rate to a cuff reading. The cuff's
#: deflation takes 30-60 s and the PPG figure is a per-minute mean, so a minute
#: either side is the tightest honest match.
PAIR_HR_WINDOW_S = 90.0


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
    }


def cuff_points(
    con: duckdb.DuckDBPyConnection, *, tz: str, days: int | None = None
) -> list[dict[str, Any]]:
    """Cuff readings as epoch-stamped points.

    ``taken_at`` is the cuff's local wall time, so it is converted through the
    zone rather than assumed to be UTC, and corrected by the per-reading clock
    offset. The stored timestamp is never rewritten -- the dedup identity depends
    on it -- so the correction happens here, at read time, as designed.
    """
    if "cuff_readings" not in _tables(con):
        return []
    where = ["coalesce(clock_suspect, FALSE) = FALSE"]
    params: list[Any] = [tz]
    if days:
        where.append(
            f"epoch(timezone(?, taken_at::TIMESTAMP)) >= epoch(now()) - {int(days) * 86400}"
        )
        params.append(tz)
    rows = con.execute(
        f"""
        SELECT epoch(timezone(?, taken_at::TIMESTAMP)) - coalesce(clock_offset_s, 0) AS ts,
               sys, dia, pulse,
               coalesce(uploader_phone_id, 'unknown') AS subject_id,
               coalesce(clock_valid, FALSE) AS clock_ok,
               coalesce(ihb, FALSE) AS irregular
        FROM cuff_readings
        WHERE {" AND ".join(where)}
        ORDER BY ts
        LIMIT {MAX_CUFF_POINTS}
        """,
        params,
    ).fetchall()
    return [
        {
            "ts": r[0],
            "sys": r[1],
            "dia": r[2],
            "pulse": r[3],
            "subject_id": r[4],
            "clock_ok": bool(r[5]),
            "irregular": bool(r[6]),
        }
        for r in rows
    ]


def coverage_days(
    con: duckdb.DuckDBPyConnection, *, tz: str, days: int = 30
) -> list[dict[str, Any]]:
    """Per-day recorded minutes and cuff-reading counts, per subject.

    A calendar grid rather than a list of sessions: the question this answers is
    "which days have no data", and absent days are the informative ones.
    """
    tables = _tables(con)
    out: dict[tuple[str, str], dict[str, Any]] = {}

    if {"sessions", "uploads"} <= tables:
        rows = con.execute(
            f"""
            SELECT strftime(timezone(?, to_timestamp(s.start_time)::TIMESTAMP),
                            '%Y-%m-%d') AS day,
                   coalesce(u.uploader_phone_id, 'unknown') AS subject_id,
                   sum(coalesce(s.end_time, s.start_time) - s.start_time) / 60.0 AS minutes,
                   count(*) AS sessions
            FROM sessions s
            LEFT JOIN uploads u ON u.phone_session_uuid = s.session_uuid
            WHERE s.start_time >= epoch(now()) - {int(days) * 86400}
            GROUP BY 1, 2
            """,
            [tz],
        ).fetchall()
        for day, sid, minutes, n in rows:
            out[(day, sid)] = {
                "day": day,
                "subject_id": sid,
                "recorded_minutes": round(float(minutes or 0), 1),
                "sessions": int(n),
                "cuff_count": 0,
            }

    if "cuff_readings" in tables:
        rows = con.execute(
            f"""
            SELECT substr(taken_at, 1, 10) AS day,
                   coalesce(uploader_phone_id, 'unknown') AS subject_id,
                   count(*) AS n
            FROM cuff_readings
            WHERE epoch(timezone(?, taken_at::TIMESTAMP)) >= epoch(now()) - {int(days) * 86400}
            GROUP BY 1, 2
            """,
            [tz],
        ).fetchall()
        for day, sid, n in rows:
            entry = out.setdefault(
                (day, sid),
                {
                    "day": day,
                    "subject_id": sid,
                    "recorded_minutes": 0.0,
                    "sessions": 0,
                    "cuff_count": 0,
                },
            )
            entry["cuff_count"] = int(n)

    return sorted(out.values(), key=lambda d: (d["day"], d["subject_id"]))


def quality_series(con: duckdb.DuckDBPyConnection, *, days: int = 30) -> list[dict[str, Any]]:
    """Hourly PPG signal quality and PPG-derived heart rate.

    Heart rate from PPG is shown because it is independently checkable against
    the cuff's pulse figure, which makes it the honest interim validation of the
    optical path. Blood pressure from PPG is not shown at all.
    """
    if "derived_ppg_minute" not in _tables(con):
        return []
    rows = con.execute(
        f"""
        SELECT epoch(date_trunc('{QUALITY_BUCKET}', to_timestamp(ts)::TIMESTAMP)) AS bucket,
               avg(sqi) AS sqi, avg(hr_ppg) AS hr, count(*) AS minutes,
               avg(acc_motion) AS motion
        FROM derived_ppg_minute
        WHERE ts >= epoch(now()) - {int(days) * 86400}
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    return [
        {
            "ts": r[0],
            "sqi": round(float(r[1]), 3) if r[1] is not None else None,
            "hr": round(float(r[2]), 1) if r[2] is not None else None,
            "minutes": int(r[3]),
            "motion": round(float(r[4]), 2) if r[4] is not None else None,
        }
        for r in rows
    ]


def pair_points(con: duckdb.DuckDBPyConnection, *, tz: str) -> list[dict[str, Any]]:
    """Cuff readings that fall inside a recording, with the PPG heart rate there.

    The seed of the calibration view. Same guards as the status pair count: same
    subject only, and a trustworthy cuff clock only, because a reading whose
    timestamp cannot be believed cannot be attached to a PPG window at all.
    """
    tables = _tables(con)
    if not {"cuff_readings", "sessions", "uploads"} <= tables:
        return []
    has_minutes = "derived_ppg_minute" in tables
    hr_expr = (
        f"""(SELECT avg(m.hr_ppg) FROM derived_ppg_minute m
             WHERE abs(m.ts - corrected.ts) <= {PAIR_HR_WINDOW_S})"""
        if has_minutes
        else "NULL"
    )
    sqi_expr = (
        f"""(SELECT avg(m.sqi) FROM derived_ppg_minute m
             WHERE abs(m.ts - corrected.ts) <= {PAIR_HR_WINDOW_S})"""
        if has_minutes
        else "NULL"
    )
    rows = con.execute(
        f"""
        WITH corrected AS (
            SELECT epoch(timezone(?, cr.taken_at::TIMESTAMP))
                     - coalesce(cr.clock_offset_s, 0) AS ts,
                   cr.sys, cr.dia, cr.pulse,
                   coalesce(cr.uploader_phone_id, 'unknown') AS subject_id
            FROM cuff_readings cr
            WHERE coalesce(cr.clock_valid, FALSE)
              AND NOT coalesce(cr.clock_suspect, FALSE)
        )
        SELECT * FROM (
            SELECT corrected.ts, corrected.sys, corrected.dia, corrected.pulse,
                   corrected.subject_id,
                   (SELECT s.id FROM sessions s
                    LEFT JOIN uploads u ON u.phone_session_uuid = s.session_uuid
                    WHERE coalesce(u.uploader_phone_id, 'unknown') = corrected.subject_id
                      AND corrected.ts BETWEEN s.start_time - 120
                                           AND coalesce(s.end_time, s.start_time) + 120
                    LIMIT 1) AS session_id,
                   {hr_expr} AS hr_ppg,
                   {sqi_expr} AS sqi
            FROM corrected
        )
        WHERE session_id IS NOT NULL
        ORDER BY ts
        """,
        [tz],
    ).fetchall()
    return [
        {
            "ts": r[0],
            "sys": r[1],
            "dia": r[2],
            "pulse": r[3],
            "subject_id": r[4],
            "session_id": r[5],
            "hr_ppg": round(float(r[6]), 1) if r[6] is not None else None,
            "sqi": round(float(r[7]), 3) if r[7] is not None else None,
        }
        for r in rows
    ]


def collect(
    con: duckdb.DuckDBPyConnection,
    *,
    timezone: str | None = None,
    days: int = 60,
) -> dict[str, Any]:
    """Everything the charts need, in one response."""
    tz = timezone or local_timezone(con)
    return {
        "timezone": tz,
        "days": days,
        "cuff": cuff_points(con, tz=tz, days=days),
        "coverage": coverage_days(con, tz=tz, days=min(days, 30)),
        "quality": quality_series(con, days=days),
        "pairs": pair_points(con, tz=tz),
    }
