"""Tests for proxy timeseries / latency features."""
from __future__ import annotations

import asyncio
import time

from testharness.proxy import ByteCountingProxy





# ---------------------------------------------------------------------------
# Snapshot delta
# ---------------------------------------------------------------------------


def test_snapshot_has_t_ms():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    proxy._start_monotonic = time.monotonic() - 1.0  # pretend started 1s ago
    snap = proxy.snapshot()
    assert "t_ms" in snap
    assert snap["t_ms"] > 0.0


def test_delta_excludes_t_ms():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    snap = proxy.snapshot()
    proxy._bytes_in = 100
    proxy._bytes_out = 200
    d = proxy.delta(snap)
    assert d == {"bytes_in": 100, "bytes_out": 200, "bytes_total": 300}


# ---------------------------------------------------------------------------
# Latency injection
# ---------------------------------------------------------------------------


def test_set_latency():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    assert proxy.latency_ms == 0
    proxy.set_latency(50)
    assert proxy.latency_ms == 50
    proxy.set_latency(0)
    assert proxy.latency_ms == 0


def test_set_latency_clamps_negative():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    proxy.set_latency(-10)
    assert proxy.latency_ms == 0


# ---------------------------------------------------------------------------
# get_timeseries math
# ---------------------------------------------------------------------------


def test_get_timeseries_empty_when_no_samples():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    proxy._start_monotonic = time.monotonic()
    snap = proxy.snapshot()
    assert proxy.get_timeseries(snap) == []


def test_get_timeseries_delta_calculation():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    proxy._start_monotonic = time.monotonic() - 10.0

    # Manually inject samples simulating bytes accumulating over time
    proxy._samples = [
        {"t_ms": 100.0, "bytes_in": 500, "bytes_out": 1000},
        {"t_ms": 300.0, "bytes_in": 700, "bytes_out": 1400},
        {"t_ms": 500.0, "bytes_in": 900, "bytes_out": 1800},
    ]

    # snap at t_ms=50, bytes_in=200, bytes_out=400
    snap = {"bytes_in": 200, "bytes_out": 400, "t_ms": 50.0}
    ts = proxy.get_timeseries(snap)

    assert len(ts) == 3

    # First point: delta from snap (200, 400) to sample (500, 1000)
    assert ts[0]["bytes_in"] == 300    # 500 - 200
    assert ts[0]["bytes_out"] == 600   # 1000 - 400
    assert ts[0]["t_ms"] == 50.0       # rebased: 100 - 50

    # Second point: delta from previous sample
    assert ts[1]["bytes_in"] == 200    # 700 - 500
    assert ts[1]["bytes_out"] == 400   # 1400 - 1000
    assert ts[1]["t_ms"] == 250.0      # rebased: 300 - 50


def test_get_timeseries_ignores_samples_before_snap():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    proxy._start_monotonic = time.monotonic() - 10.0
    proxy._samples = [
        {"t_ms": 50.0, "bytes_in": 100, "bytes_out": 200},   # BEFORE snap
        {"t_ms": 250.0, "bytes_in": 300, "bytes_out": 600},  # AFTER snap
    ]
    snap = {"bytes_in": 50, "bytes_out": 100, "t_ms": 100.0}
    ts = proxy.get_timeseries(snap)
    # Only the sample after snap should appear
    assert len(ts) == 1
    assert ts[0]["t_ms"] == 150.0   # rebased: 250 - 100


# ---------------------------------------------------------------------------
# live_timeseries
# ---------------------------------------------------------------------------


def test_live_timeseries_filters_by_time():
    proxy = ByteCountingProxy("127.0.0.1", 19419, "127.0.0.1", 19418)
    proxy._start_monotonic = time.monotonic()
    # Inject samples at various t_ms values
    proxy._samples = [
        {"t_ms": 1000.0, "bytes_in": 10, "bytes_out": 20},   # 1s ago-ish
        {"t_ms": 5000.0, "bytes_in": 50, "bytes_out": 100},  # 5s ago-ish
        {"t_ms": 55000.0, "bytes_in": 500, "bytes_out": 1000},  # 55s ago-ish
    ]
    # Force _now_ms to return 60000 (60s elapsed)
    proxy._start_monotonic -= 60.0

    recent = proxy.live_timeseries(last_seconds=10.0)
    # Only samples within last 10s (t_ms >= 50000) should appear
    assert len(recent) == 1
    assert recent[0]["bytes_in"] == 500


# ---------------------------------------------------------------------------
# Integration: proxy start/stop with sample loop
# ---------------------------------------------------------------------------


def test_proxy_sample_loop_produces_samples():
    """The sample loop should produce at least 2 samples after running for 0.55s."""
    proxy = ByteCountingProxy("127.0.0.1", 19900, "127.0.0.1", 19901)
    proxy._start_monotonic = time.monotonic()

    async def _run():
        task = asyncio.get_event_loop().create_task(proxy._sample_loop())
        await asyncio.sleep(0.55)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    # Should have at least 2 samples (0.2s interval × 0.55s ≈ 2-3 samples)
    assert len(proxy._samples) >= 2
    for s in proxy._samples:
        assert "t_ms" in s
        assert "bytes_in" in s
        assert "bytes_out" in s
