"""Per-session raw waveform views: PPG, smoothed ACC/GYRO motion, and a
beat-by-beat heart-rate decomposition from the PPG peaks.

This is a visualization feature, not part of the BP-estimation pipeline (that
lives in the separate ``polar-ppg-bp`` repo and is intentionally not imported
here -- this service should not depend on it). The filtering below is a
lightweight numpy-only bandpass rather than a real Butterworth/scipy filter,
because the goal here is "does this look like a clean pulse" for a person
watching a live calibration visit, not a scientific measurement -- scipy is a
heavier dependency than this view justifies.

Scoping follows the same rule as everything else in this module: a session
belongs to a subject via sessions -> uploads.uploader_phone_id, and a scoped
viewer may only request a session_id in their own allowed set (see
status._allowed_session_ids). There is no separate access check here beyond
that session_id must appear in the caller-supplied allowed list -- the caller
(web.py) is responsible for passing it in.
"""

from __future__ import annotations

from typing import Any

import duckdb
import numpy as np

#: Hard cap on points returned per channel. A 40-minute PPG recording at 176 Hz
#: is ~420k samples; no browser chart benefits from more than a couple thousand
#: points, and shipping the raw count would be several MB of JSON for one chart.
MAX_POINTS = 2000

#: Approximate native sample rates, used only to size the smoothing window for
#: ACC/GYRO motion magnitude. Not trusted for anything quantitative -- the real
#: rate is derived from actual timestamps, same principle as the BP-side ETL.
ACC_GYRO_SMOOTH_S = 0.5


def _f(value: Any, digits: int = 3) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v, digits)


def _fs_from_timestamps(ts_ns: np.ndarray) -> float:
    """Real sample rate from deduped consecutive gaps, not the nominal spec.

    Mirrors the same reasoning used in polar-ppg-bp's own ETL: a nominal rate
    (176 Hz for PPG, 416 Hz for ACC/GYRO) is what the sensor was asked for, not
    what it necessarily delivered -- BLE drops and buffering jitter mean the
    real rate should come from the data.
    """
    if len(ts_ns) < 2:
        return 1.0
    diffs = np.diff(ts_ns).astype(np.float64)
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        return 1.0
    return float(1e9 / np.median(diffs))


def _envelope_downsample(x: np.ndarray, t: np.ndarray, target_points: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Min/max envelope decimation: each output bucket keeps the min and max of
    the samples it covers, so a downsampled PPG trace still shows every peak
    instead of aliasing them away like a plain stride would.

    Returns (t_out, lo, hi) with lo/hi interleaved by the caller as needed.
    """
    n = len(x)
    if n <= target_points:
        return t, x, x
    bucket = int(np.ceil(n / target_points))
    n_buckets = int(np.ceil(n / bucket))
    t_out = np.empty(n_buckets)
    lo = np.empty(n_buckets)
    hi = np.empty(n_buckets)
    for i in range(n_buckets):
        s, e = i * bucket, min((i + 1) * bucket, n)
        seg = x[s:e]
        lo[i] = seg.min()
        hi[i] = seg.max()
        t_out[i] = t[s + (e - s) // 2]
    return t_out, lo, hi


def _bandpass_ppg(x: np.ndarray, fs: float) -> np.ndarray:
    """Numpy-only pulse-band isolation: remove slow baseline drift (motion,
    venous return) with a wide moving-average subtraction, then remove
    high-frequency sensor noise with a short moving average. Not a real
    Butterworth filter -- see module docstring for why that tradeoff is fine
    here."""
    x = x.astype(np.float64)
    baseline_win = max(3, int(fs * 1.5) | 1)  # ~1.5s, odd for a centered window
    noise_win = max(1, int(fs * 0.05) | 1)  # ~50ms
    baseline = _moving_average(x, baseline_win)
    denoised = _moving_average(x, noise_win)
    return denoised - baseline


def _moving_average(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return x.copy()
    kernel = np.ones(window) / window
    pad = window // 2
    padded = np.pad(x, pad, mode="edge")
    return np.convolve(padded, kernel, mode="valid")[: len(x)]


def _detect_peaks(x: np.ndarray, fs: float) -> np.ndarray:
    """Simple local-maximum peak detector with a minimum-distance constraint,
    good enough to find systolic peaks in a filtered PPG signal without
    scipy.signal.find_peaks."""
    if len(x) < 3:
        return np.array([], dtype=int)
    min_dist = max(1, int(0.35 * fs))  # ~170 BPM ceiling
    thresh = np.std(x) * 0.3
    candidates = np.where((x[1:-1] > x[:-2]) & (x[1:-1] > x[2:]) & (x[1:-1] > thresh))[0] + 1
    if len(candidates) == 0:
        return candidates
    peaks = [int(candidates[0])]
    for c in candidates[1:]:
        if c - peaks[-1] >= min_dist:
            peaks.append(int(c))
        elif x[c] > x[peaks[-1]]:
            peaks[-1] = int(c)  # keep the taller of two close candidates
    return np.array(peaks, dtype=int)


def session_waveform(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: int,
    allowed_session_ids: list[int] | None,
) -> dict[str, Any]:
    """Raw-ish PPG/ACC/GYRO for one session plus a beat-by-beat HR decomposition.

    ``allowed_session_ids`` is the caller's already-scoped list (None means
    unrestricted); a session_id outside that list gets an empty, not-found-shaped
    response rather than a 403 that would confirm the session exists at all.
    """
    if allowed_session_ids is not None and session_id not in allowed_session_ids:
        return {"session_id": session_id, "found": False}

    sess = con.execute(
        "SELECT id, start_time, epoch_offset_ns, device_name FROM sessions WHERE id = ?",
        [session_id],
    ).fetchone()
    if sess is None:
        return {"session_id": session_id, "found": False}
    _, start_time, epoch_offset_ns, device_name = sess

    out: dict[str, Any] = {
        "session_id": session_id,
        "found": True,
        "start_time": _f(start_time),
        "device_name": device_name,
        "ppg": None,
        "acc": None,
        "gyro": None,
        "hr": None,
    }

    ppg_df = con.execute(
        "SELECT timestamp_ns, ppg0, ppg1, ppg2, ambient FROM ppg "
        "WHERE session_id = ? ORDER BY timestamp_ns",
        [session_id],
    ).df()
    if len(ppg_df):
        ts_ns = ppg_df["timestamp_ns"].to_numpy()
        fs = _fs_from_timestamps(ts_ns)
        t_s = (ts_ns - ts_ns[0]) / 1e9
        # Ambient-subtracted channels, pick whichever has the most signal energy
        # (same "strongest pulse wins" idea as the BP-side ETL's channel choice).
        chans = np.stack(
            [ppg_df[c].to_numpy(dtype=np.float64) - ppg_df["ambient"].to_numpy(dtype=np.float64)
             for c in ("ppg0", "ppg1", "ppg2")],
            axis=1,
        )
        filtered = np.stack([_bandpass_ppg(chans[:, i], fs) for i in range(3)], axis=1)
        energies = filtered.var(axis=0)
        best = int(np.argmax(energies))
        sig = filtered[:, best]

        t_out, lo, hi = _envelope_downsample(sig, t_s, MAX_POINTS)
        out["ppg"] = {
            "fs_hz": _f(fs, 1),
            "channel": best,
            "t_s": [round(float(v), 3) for v in t_out],
            "lo": [_f(v) for v in lo],
            "hi": [_f(v) for v in hi],
        }

        peaks = _detect_peaks(sig, fs)
        if len(peaks) >= 2:
            peak_t = t_s[peaks]
            ibi_s = np.diff(peak_t)
            hr_bpm = 60.0 / ibi_s
            # Drop physiologically impossible beats (sensor glitch / motion
            # artifact merged two peaks or split one), rather than let one bad
            # interval spike the whole HR trace.
            valid = (hr_bpm > 30) & (hr_bpm < 220)
            hr_t = peak_t[1:][valid]
            hr_vals = hr_bpm[valid]
            if len(hr_vals) > MAX_POINTS:
                idx = np.linspace(0, len(hr_vals) - 1, MAX_POINTS).astype(int)
                hr_t, hr_vals = hr_t[idx], hr_vals[idx]
            out["hr"] = {
                "t_s": [round(float(v), 3) for v in hr_t],
                "bpm": [_f(v, 1) for v in hr_vals],
                "n_beats": int(len(peaks)),
                "mean_bpm": _f(float(np.mean(hr_bpm[valid])), 1) if valid.any() else None,
            }

    for table, out_key in (("acc", "acc"), ("gyro", "gyro")):
        df = con.execute(
            f"SELECT timestamp_ns, x, y, z FROM {table} WHERE session_id = ? ORDER BY timestamp_ns",
            [session_id],
        ).df()
        if not len(df):
            continue
        ts_ns = df["timestamp_ns"].to_numpy()
        fs = _fs_from_timestamps(ts_ns)
        t_s = (ts_ns - ts_ns[0]) / 1e9
        mag = np.sqrt(
            df["x"].to_numpy(dtype=np.float64) ** 2
            + df["y"].to_numpy(dtype=np.float64) ** 2
            + df["z"].to_numpy(dtype=np.float64) ** 2
        )
        win = max(1, int(fs * ACC_GYRO_SMOOTH_S) | 1)
        smoothed = _moving_average(mag, win)
        t_out, lo, hi = _envelope_downsample(smoothed, t_s, MAX_POINTS)
        out[out_key] = {
            "fs_hz": _f(fs, 1),
            "t_s": [round(float(v), 3) for v in t_out],
            "lo": [_f(v) for v in lo],
            "hi": [_f(v) for v in hi],
        }

    return out
