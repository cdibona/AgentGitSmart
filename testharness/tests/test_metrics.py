"""Tests for testharness.metrics: merge_timeseries, CpuSampler."""
from __future__ import annotations

import os
import time

import pytest

from testharness.metrics import CpuSampler, merge_timeseries


# ---------------------------------------------------------------------------
# merge_timeseries
# ---------------------------------------------------------------------------


def test_merge_timeseries_empty():
    assert merge_timeseries([], []) == []


def test_merge_timeseries_no_bytes_returns_cpu_grid():
    cpu = [{"t_ms": 100.0, "cpu_pct": 50.0}, {"t_ms": 300.0, "cpu_pct": 30.0}]
    result = merge_timeseries([], cpu)
    assert len(result) == 2
    assert result[0] == {"t_ms": 100.0, "bytes_in": 0, "bytes_out": 0, "cpu_pct": 50.0}
    assert result[1] == {"t_ms": 300.0, "bytes_in": 0, "bytes_out": 0, "cpu_pct": 30.0}


def test_merge_timeseries_bytes_only():
    bps = [
        {"t_ms": 100.0, "bytes_in": 100, "bytes_out": 200},
        {"t_ms": 300.0, "bytes_in": 150, "bytes_out": 300},
    ]
    result = merge_timeseries(bps, [])
    assert len(result) == 2
    assert result[0]["cpu_pct"] == 0.0
    assert result[0]["bytes_out"] == 200


def test_merge_timeseries_nearest_join():
    bps = [
        {"t_ms": 100.0, "bytes_in": 10, "bytes_out": 20},
        {"t_ms": 300.0, "bytes_in": 30, "bytes_out": 60},
    ]
    cpu = [
        {"t_ms": 90.0, "cpu_pct": 55.0},   # nearest to 100 → delta 10ms
        {"t_ms": 310.0, "cpu_pct": 22.0},  # nearest to 300 → delta 10ms
    ]
    result = merge_timeseries(bps, cpu)
    assert len(result) == 2
    assert result[0]["cpu_pct"] == 55.0
    assert result[1]["cpu_pct"] == 22.0


def test_merge_timeseries_outside_window_gets_zero():
    """CPU point > 150ms away from byte point should NOT be merged."""
    bps = [{"t_ms": 100.0, "bytes_in": 5, "bytes_out": 10}]
    cpu = [{"t_ms": 400.0, "cpu_pct": 99.9}]  # 300ms away
    result = merge_timeseries(bps, cpu)
    assert result[0]["cpu_pct"] == 0.0


def test_merge_timeseries_output_shape():
    bps = [{"t_ms": float(i * 200), "bytes_in": i * 10, "bytes_out": i * 20} for i in range(5)]
    cpu = [{"t_ms": float(i * 200 + 10), "cpu_pct": float(i * 5)} for i in range(5)]
    result = merge_timeseries(bps, cpu)
    assert len(result) == 5
    for p in result:
        assert set(p.keys()) == {"t_ms", "bytes_in", "bytes_out", "cpu_pct"}


# ---------------------------------------------------------------------------
# CpuSampler — pidtree kind (requires psutil)
# ---------------------------------------------------------------------------


try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


@pytest.mark.skipif(not _PSUTIL_AVAILABLE, reason="psutil not installed")
def test_cpusampler_pidtree_produces_samples():
    sampler = CpuSampler(kind="pidtree", root_pid=os.getpid(), interval_s=0.05)
    sampler.start()
    # Run some trivial CPU work so there is something to sample
    _ = sum(range(100_000))
    time.sleep(0.3)
    sampler.stop()

    samples = sampler.samples
    assert len(samples) > 0, "Expected at least one CPU sample"
    for s in samples:
        assert "t_ms" in s
        assert "cpu_pct" in s
        assert s["cpu_pct"] >= 0.0


@pytest.mark.skipif(not _PSUTIL_AVAILABLE, reason="psutil not installed")
def test_cpusampler_on_sample_callback():
    collected: list = []

    def on_sample(t_ms: float, pct: float) -> None:
        collected.append((t_ms, pct))

    sampler = CpuSampler(
        kind="pidtree", root_pid=os.getpid(), interval_s=0.05, on_sample=on_sample
    )
    sampler.start()
    time.sleep(0.25)
    sampler.stop()

    assert len(collected) > 0
    assert all(isinstance(t, float) for t, _ in collected)


@pytest.mark.skipif(not _PSUTIL_AVAILABLE, reason="psutil not installed")
def test_cpusampler_peak_pct():
    sampler = CpuSampler(kind="pidtree", root_pid=os.getpid(), interval_s=0.05)
    sampler.start()
    time.sleep(0.25)
    sampler.stop()

    assert sampler.peak_pct >= 0.0
    if sampler.samples:
        assert sampler.peak_pct == max(s["cpu_pct"] for s in sampler.samples)
