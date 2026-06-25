"""Tests for testharness.agent_task: run_agent_task with naive approach."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from testharness.agent_task import run_agent_task


# ---------------------------------------------------------------------------
# Helper: create a minimal git repo in tmp_path
# ---------------------------------------------------------------------------


def _make_git_repo(path: str) -> None:
    """Create a minimal git repo with a few Python files at *path*."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "test@test.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "Test"],
                   check=True, capture_output=True)

    # Write a few files
    for name, content in [
        ("src/app.py", "class MyApp:\n    pass\n"),
        ("src/util.py", "class Helper:\n    pass\n"),
        ("README.md", "# test\n"),
    ]:
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "commit", "-m", "init"],
                   check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_agent_task_naive_returns_expected_keys(tmp_path):
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="naive",
        workspace=workspace,
        commit="deadbeef" * 5,   # fake commit, not used by naive
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="MyApp",
    )

    # Check all required keys are present
    assert "symbol_lookup_ms" in result
    assert "file_read_ms" in result
    assert "network_roundtrips" in result
    assert "total_agent_ready_ms" in result
    assert "grep_cpu_pct" in result
    assert "detail" in result


def test_run_agent_task_naive_types(tmp_path):
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="naive",
        workspace=workspace,
        commit="deadbeef" * 5,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="MyApp",
    )

    assert isinstance(result["symbol_lookup_ms"], float)
    assert isinstance(result["file_read_ms"], float)
    assert isinstance(result["network_roundtrips"], int)
    assert isinstance(result["total_agent_ready_ms"], float)
    assert isinstance(result["grep_cpu_pct"], float)
    assert isinstance(result["detail"], dict)


def test_run_agent_task_naive_no_network_roundtrips(tmp_path):
    """Naive approach does everything locally — zero network round-trips."""
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="naive",
        workspace=workspace,
        commit="deadbeef" * 5,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="MyApp",
    )
    assert result["network_roundtrips"] == 0


def test_run_agent_task_naive_positive_timings(tmp_path):
    """All timing values should be non-negative."""
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="naive",
        workspace=workspace,
        commit="deadbeef" * 5,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="MyApp",
    )
    assert result["symbol_lookup_ms"] >= 0.0
    assert result["file_read_ms"] >= 0.0
    assert result["total_agent_ready_ms"] >= 0.0
    assert result["grep_cpu_pct"] >= 0.0


def test_run_agent_task_naive_detail_fields(tmp_path):
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="naive",
        workspace=workspace,
        commit="deadbeef" * 5,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="MyApp",
    )

    detail = result["detail"]
    assert "approach" in detail
    assert detail["approach"] == "naive"
    assert "files_read" in detail
    assert "symbol_hits" in detail
    assert isinstance(detail["files_read"], int)
    assert detail["files_read"] >= 0
    assert isinstance(detail["symbol_hits"], int)
    assert detail["symbol_hits"] >= 0


def test_run_agent_task_returns_without_crashing_for_missing_workspace(tmp_path):
    """Should not raise even if workspace is empty / missing files."""
    result = run_agent_task(
        approach="naive",
        workspace=str(tmp_path / "nonexistent_dir"),
        commit="0" * 40,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="ClassDef",
    )
    # Should still return a valid dict with expected keys
    assert "total_agent_ready_ms" in result
    assert result["network_roundtrips"] == 0


def test_run_agent_task_blobless_returns_valid_structure(tmp_path):
    """Blobless approach should return a valid result dict (may have 0 roundtrips if no extra files)."""
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="blobless",
        workspace=workspace,
        commit="deadbeef" * 5,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="Helper",
    )

    assert "total_agent_ready_ms" in result
    assert "network_roundtrips" in result
    assert result["total_agent_ready_ms"] >= 0.0


def test_run_agent_task_agentcache_falls_back_gracefully(tmp_path):
    """AgentCache should not crash even when service is unreachable."""
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    # Service on port 19999 is not running — should handle gracefully
    result = run_agent_task(
        approach="agentcache",
        workspace=workspace,
        commit="0" * 40,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:19999",
        symbol="MyApp",
    )

    # Should return valid structure even on service failure
    assert "total_agent_ready_ms" in result
    assert result["total_agent_ready_ms"] >= 0.0


def test_run_agent_task_is_json_serializable(tmp_path):
    """The result must be serializable to JSON (for use in container_entry output)."""
    workspace = str(tmp_path)
    _make_git_repo(workspace)

    result = run_agent_task(
        approach="naive",
        workspace=workspace,
        commit="deadbeef" * 5,
        target_paths=["src/app.py"],
        service_url="http://127.0.0.1:8765",
        symbol="MyApp",
    )

    serialized = json.dumps(result)  # must not raise
    deserialized = json.loads(serialized)
    assert deserialized["network_roundtrips"] == result["network_roundtrips"]
