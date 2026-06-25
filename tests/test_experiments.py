"""Tests for the experiment suite (lightweight; no big-repo / network paths)."""
from __future__ import annotations

from experiments import exp3_hook_update
from experiments.harness import RunResult, fmt_bytes


def test_fmt_bytes_units():
    assert fmt_bytes(0) == "0.0 B"
    assert fmt_bytes(1024) == "1.0 KiB"
    assert fmt_bytes(1024 * 1024) == "1.0 MiB"
    assert fmt_bytes(5 * 1024 * 1024 * 1024) == "5.0 GiB"


def test_runresult_to_dict_roundtrip():
    r = RunResult(
        repo="redis", method="agentcache", iteration=1, seed=42,
        cache_existed_before=False, cache_refs_before=0, cache_refs_after=1,
        cache_built_this_run=True, bytes_proxy_out=1234, bytes_proxy_in=10,
        wall_s=0.5,
    )
    d = r.to_dict()
    assert d["repo"] == "redis"
    assert d["cache_built_this_run"] is True
    assert d["bytes_proxy_out"] == 1234


def test_exp3_hook_update_passes():
    """The post-receive hook builds a cache on each human push (synthetic repo)."""
    report = exp3_hook_update.run(branch="main")
    assert report["verdict"] == "PASS"
    steps = {s["step"]: s for s in report["steps"]}

    # First push built a cache whose manifest includes the human's new file.
    assert steps["first_push"]["cache_built"] is True
    assert steps["first_push"]["edited_path_in_manifest"] is True

    # Second push produced a second, distinct cache ref.
    assert steps["second_push"]["cache_built"] is True
    assert steps["second_push"]["total_cache_refs"] == 2
