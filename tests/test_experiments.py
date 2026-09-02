"""Tests for the experiment suite (lightweight; no big-repo / network paths)."""

from __future__ import annotations

import random

from experiments import exp3_hook_update
from experiments.harness import RunResult, fmt_bytes
from testharness.real_agent import (
    SOURCE_EXTS,
    _add_exclamation,
    _comment_prefixes,
)
from agentgitsmart import symbols as sym


def test_comment_prefixes_by_language():
    # '#' family
    assert _comment_prefixes("a/b/foo.py") == ("#",)
    assert _comment_prefixes("plugins/git.zsh") == ("#",)
    # '//' family — the cases that used to break (Rust/Go/JS/C)
    assert _comment_prefixes("src/main.rs") == ("//",)
    assert _comment_prefixes("cmd/x.go") == ("//",)
    assert _comment_prefixes("index.tsx") == ("//",)
    assert _comment_prefixes("util.c") == ("//",)
    # other markers
    assert _comment_prefixes("init.lua") == ("--",)
    # unknown / extensionless
    assert _comment_prefixes("Makefile") == ()


def test_add_exclamation_slashslash_comment():
    """The Rust/Go/JS path: '//' comments must be editable, not just '#'."""
    src = "fn main() {\n    // a comment\n    let x = 1;\n}\n"
    out, changed = _add_exclamation(src, random.Random(0), ("//",))
    assert changed is True
    assert "// a comment!" in out


def test_add_exclamation_no_eligible_comment():
    out, changed = _add_exclamation("let x = 1;\n", random.Random(0), ("//",))
    assert changed is False


def test_source_exts_cover_polyglot_fleet():
    for ext in (".py", ".rs", ".go", ".js", ".ts", ".c", ".lua", ".rb", ".sh"):
        assert ext in SOURCE_EXTS


def test_fmt_bytes_units():
    assert fmt_bytes(0) == "0.0 B"
    assert fmt_bytes(1024) == "1.0 KiB"
    assert fmt_bytes(1024 * 1024) == "1.0 MiB"
    assert fmt_bytes(5 * 1024 * 1024 * 1024) == "5.0 GiB"


def test_runresult_to_dict_roundtrip():
    r = RunResult(
        repo="redis",
        method="agentgitsmart",
        iteration=1,
        seed=42,
        cache_existed_before=False,
        cache_refs_before=0,
        cache_refs_after=1,
        cache_built_this_run=True,
        bytes_proxy_out=1234,
        bytes_proxy_in=10,
        wall_s=0.5,
    )
    d = r.to_dict()
    assert d["repo"] == "redis"
    assert d["cache_built_this_run"] is True
    assert d["bytes_proxy_out"] == 1234


def test_exp3_hook_update_passes(tmp_path):
    """The post-receive hook builds a cache on each human push (synthetic repo)."""
    # out_dir keeps the report in tmp_path; without it the test overwrites the
    # committed experiments/results/exp3_hook_update.json.
    report = exp3_hook_update.run(branch="main", out_dir=tmp_path)
    assert report["verdict"] == "PASS"
    steps = {s["step"]: s for s in report["steps"]}

    # First push built a cache whose manifest includes the human's new file.
    assert steps["first_push"]["cache_built"] is True
    assert steps["first_push"]["edited_path_in_manifest"] is True

    # Second push produced a second, distinct cache ref.
    assert steps["second_push"]["cache_built"] is True
    assert steps["second_push"]["total_cache_refs"] == 2

    # Load observability: first push must always be a full build.
    first_load = steps["first_push"]["load"]
    assert first_load.get("mode") == "full"

    # Load observability: second push is delta when ctags is available,
    # otherwise it degrades gracefully to full.
    second_load = steps["second_push"]["load"]
    if sym.ctags_available():
        assert second_load.get("mode") == "delta", (
            f"Expected delta for second push (ctags available), got: {second_load}"
        )
        assert second_load.get("files_reindexed") == 1, (
            f"Expected files_reindexed==1 for second push, got: {second_load}"
        )
    else:
        assert second_load.get("mode") == "full", (
            f"Expected full for second push (ctags absent), got: {second_load}"
        )
