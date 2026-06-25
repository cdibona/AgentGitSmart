"""Tests for testharness.models: new fields, backward-compatible deserialization."""
from __future__ import annotations

import pytest

from testharness.models import (
    AgentTaskMetrics,
    ApproachResult,
    RunConfig,
    RunDetail,
    SystemStatus,
    TimeseriesPoint,
)


# ---------------------------------------------------------------------------
# ApproachResult
# ---------------------------------------------------------------------------


def test_approach_result_defaults():
    r = ApproachResult(approach="naive", elapsed_s=1.5)
    assert r.clone_ms == 0.0
    assert r.timeseries == []
    assert r.agent_task is None
    assert r.used_docker is False
    assert r.latency_ms == 0


def test_approach_result_roundtrip_with_new_fields():
    ts = [TimeseriesPoint(t_ms=100.0, bytes_in=1024, bytes_out=2048, cpu_pct=50.0)]
    agent = AgentTaskMetrics(
        symbol_lookup_ms=10.5,
        file_read_ms=5.0,
        network_roundtrips=2,
        total_agent_ready_ms=20.0,
        grep_cpu_pct=30.0,
    )
    r = ApproachResult(
        approach="agentcache",
        elapsed_s=2.5,
        clone_ms=2300.0,
        timeseries=ts,
        agent_task=agent,
        used_docker=True,
        latency_ms=50,
    )
    dumped = r.model_dump()
    restored = ApproachResult(**dumped)

    assert restored.approach == "agentcache"
    assert restored.clone_ms == 2300.0
    assert restored.used_docker is True
    assert restored.latency_ms == 50
    assert len(restored.timeseries) == 1
    assert restored.timeseries[0].bytes_out == 2048
    assert restored.agent_task is not None
    assert restored.agent_task.network_roundtrips == 2


def test_approach_result_old_style_dict():
    """Old-style dicts without new fields must still parse (backward compat)."""
    old_dict = {
        "approach": "naive",
        "elapsed_s": 4.0,
        "bytes_proxy_in": 100,
        "bytes_proxy_out": 200,
        "bytes_proxy_total": 300,
        "objects_received": 10,
        "disk_bytes": 50000,
        "file_count": 5,
        # no clone_ms, timeseries, agent_task, used_docker, latency_ms
    }
    r = ApproachResult(**old_dict)
    assert r.clone_ms == 0.0
    assert r.timeseries == []
    assert r.agent_task is None
    assert r.used_docker is False


# ---------------------------------------------------------------------------
# RunConfig
# ---------------------------------------------------------------------------


def test_runconfig_defaults():
    cfg = RunConfig(repo_name="cpython.git", target_paths=["Lib/ast.py"])
    assert cfg.use_docker is True
    assert cfg.latency_ms == 0
    assert cfg.num_runs == 3


def test_runconfig_with_docker_latency():
    cfg = RunConfig(
        repo_name="cpython.git",
        target_paths=["Lib/ast.py"],
        use_docker=False,
        latency_ms=50,
    )
    assert cfg.use_docker is False
    assert cfg.latency_ms == 50


# ---------------------------------------------------------------------------
# SystemStatus
# ---------------------------------------------------------------------------


def test_system_status_docker_field():
    s = SystemStatus(docker_available=True, repos=["cpython.git"])
    assert s.docker_available is True
    assert s.repos == ["cpython.git"]


def test_system_status_defaults():
    s = SystemStatus()
    assert s.docker_available is False


# ---------------------------------------------------------------------------
# TimeseriesPoint / AgentTaskMetrics
# ---------------------------------------------------------------------------


def test_timeseries_point():
    p = TimeseriesPoint(t_ms=500.0, bytes_in=100, bytes_out=200, cpu_pct=33.3)
    assert p.t_ms == 500.0
    assert p.bytes_in == 100


def test_agent_task_metrics_defaults():
    m = AgentTaskMetrics()
    assert m.symbol_lookup_ms == 0.0
    assert m.network_roundtrips == 0


# ---------------------------------------------------------------------------
# RunDetail
# ---------------------------------------------------------------------------


def test_run_detail_new_fields():
    rd = RunDetail(
        run_id="abc123",
        created_at="2025-01-01T00:00:00Z",
        status="complete",
        repo_name="cpython.git",
        branch="main",
        target_paths=["Lib/ast.py"],
        approaches=["naive"],
        use_docker=True,
        latency_ms=20,
    )
    assert rd.use_docker is True
    assert rd.latency_ms == 20
