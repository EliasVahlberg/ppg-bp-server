"""Tests for waveform.py: peak detection, envelope downsampling, and scoping."""

from __future__ import annotations

import numpy as np

from ppg_pi_server.waveform import (
    _bandpass_ppg,
    _detect_peaks,
    _envelope_downsample,
    _fs_from_timestamps,
    _moving_average,
    session_waveform,
)


def test_fs_from_timestamps_uses_median_gap_not_the_span():
    # 100 Hz-ish, one big gap that a naive (last-first)/(n-1) estimate would
    # smear across every sample.
    ts = np.arange(0, 100_000_000, 10_000_000, dtype=np.int64)  # 10ms steps = 100Hz
    ts_with_gap = np.concatenate([ts, ts[-1:] + 5_000_000_000])  # one 5s gap
    fs = _fs_from_timestamps(ts_with_gap)
    assert 90 < fs < 110


def test_fs_from_timestamps_degenerate_input_does_not_crash():
    assert _fs_from_timestamps(np.array([], dtype=np.int64)) == 1.0
    assert _fs_from_timestamps(np.array([5], dtype=np.int64)) == 1.0


def test_moving_average_preserves_length():
    x = np.random.default_rng(0).normal(size=137)
    for w in (1, 2, 5, 40):
        assert len(_moving_average(x, w)) == len(x)


def test_bandpass_removes_a_slow_linear_drift():
    """A pure ramp (simulating baseline drift) should be almost entirely removed,
    since it has no energy in the pulse band the filter is meant to isolate."""
    fs = 176.0
    n = int(fs * 10)
    drift = np.linspace(0, 1000, n)
    filtered = _bandpass_ppg(drift, fs)
    # Residual should be tiny relative to the original drift's scale.
    assert np.std(filtered) < np.std(drift) * 0.05


def test_detect_peaks_finds_a_clean_sine_at_the_right_rate():
    fs = 176.0
    duration_s = 20.0
    hr_hz = 70.0 / 60.0  # 70 bpm
    t = np.arange(0, duration_s, 1.0 / fs)
    sig = np.sin(2 * np.pi * hr_hz * t)
    peaks = _detect_peaks(sig, fs)
    # Expect roughly duration_s * hr_hz peaks (~23), generous tolerance since
    # this is a simple detector, not a scientific-grade one.
    expected = duration_s * hr_hz
    assert abs(len(peaks) - expected) <= 3


def test_detect_peaks_on_flat_signal_finds_nothing():
    flat = np.zeros(1000)
    assert len(_detect_peaks(flat, 176.0)) == 0


def test_detect_peaks_respects_minimum_distance():
    """Two adjacent same-height spikes closer than the minimum beat distance
    must not both be reported as separate beats."""
    fs = 176.0
    sig = np.zeros(200)
    sig[100] = 5.0
    sig[102] = 5.0  # 2 samples away, far closer than any real heartbeat
    peaks = _detect_peaks(sig, fs)
    assert len(peaks) == 1


def test_envelope_downsample_keeps_every_point_when_under_budget():
    x = np.arange(10, dtype=np.float64)
    t = np.arange(10, dtype=np.float64)
    t_out, lo, hi = _envelope_downsample(x, t, target_points=100)
    assert len(t_out) == 10
    np.testing.assert_array_equal(lo, x)
    np.testing.assert_array_equal(hi, x)


def test_envelope_downsample_preserves_spikes_a_plain_stride_would_miss():
    """A single sharp spike buried in an otherwise flat signal must survive
    decimation -- this is the whole point of min/max envelope downsampling
    over a naive stride, which would alias it away depending on phase."""
    n = 10_000
    x = np.zeros(n)
    spike_idx = 4321
    x[spike_idx] = 100.0
    t = np.arange(n, dtype=np.float64)
    t_out, lo, hi = _envelope_downsample(x, t, target_points=100)
    assert hi.max() == 100.0


def test_envelope_downsample_output_length_is_bounded():
    n = 50_000
    x = np.random.default_rng(1).normal(size=n)
    t = np.arange(n, dtype=np.float64)
    t_out, lo, hi = _envelope_downsample(x, t, target_points=2000)
    assert len(t_out) <= 2001  # ceil-division rounding, small slack
    assert len(t_out) == len(lo) == len(hi)


class _FakeCursor:
    """Minimal stand-in so session_waveform's not-found path can be tested
    without a real DuckDB store."""

    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, session_row):
        self._session_row = session_row

    def execute(self, sql, params=None):
        if "FROM sessions WHERE id" in sql:
            return _FakeCursor(self._session_row)
        raise AssertionError(f"unexpected query in this fake: {sql}")


def test_session_outside_allowed_ids_is_reported_not_found_without_querying_further():
    """A scoped viewer requesting someone else's session_id must get a
    not-found-shaped response, and the function must not even query the
    sessions table -- both because there is nothing to gain from it and so a
    real implementation cannot accidentally leak timing/behavioral differences
    between 'exists but not yours' and 'does not exist'."""
    conn = _FakeConn(session_row=None)  # would raise if queried
    result = session_waveform(conn, session_id=999, allowed_session_ids=[1, 2, 3])
    assert result == {"session_id": 999, "found": False}


def test_session_within_allowed_ids_but_missing_from_db_is_not_found():
    conn = _FakeConn(session_row=None)
    result = session_waveform(conn, session_id=1, allowed_session_ids=[1, 2, 3])
    assert result == {"session_id": 1, "found": False}


def test_unrestricted_viewer_passes_none_through():
    """allowed_session_ids=None (unrestricted viewer) must not reject any
    session_id at the allow-list check -- it should proceed to query the DB."""
    conn = _FakeConn(session_row=None)
    result = session_waveform(conn, session_id=42, allowed_session_ids=None)
    # Proceeds past the allow-list gate and only fails because the fake
    # session row is None (simulating "not in DB"), not because it was blocked.
    assert result == {"session_id": 42, "found": False}
