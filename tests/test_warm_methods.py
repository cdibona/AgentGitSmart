"""Tests for the warm_method comparison dimension in the experiment harness.

Covers:
  - ExperimentConfig.warm_method default and explicit values.
  - run_action_generate() builds a real cache ref via the github-action
    equivalent subprocess (scripts/generate_agentgitsmart.py) and returns a
    well-formed result dict.

These tests are Docker-free and fast: run_action_generate spawns a subprocess
that operates on an ephemeral bare repo (tmp_path) with no network access.
"""

from __future__ import annotations

from testharness.experiment_runner import run_action_generate
from testharness.models import ExperimentConfig
from tests.conftest import make_commit


# ---------------------------------------------------------------------------
# Config field tests (instantiation-only; no subprocess, trivially fast)
# ---------------------------------------------------------------------------


def test_warm_method_config_default():
    """ExperimentConfig.warm_method defaults to 'hook' — backward-compatible."""
    cfg = ExperimentConfig(repos=["testrepo"], methods=["agentgitsmart"])
    assert cfg.warm_method == "hook"


def test_warm_method_config_explicit_values():
    """ExperimentConfig accepts all three valid warm_method strings."""
    for value in ("hook", "action", "both"):
        cfg = ExperimentConfig(
            repos=["testrepo"], methods=["agentgitsmart"], warm_method=value
        )
        assert cfg.warm_method == value, f"Expected {value!r}, got {cfg.warm_method!r}"


# ---------------------------------------------------------------------------
# run_action_generate() — subprocess round-trip against a real ephemeral repo
# ---------------------------------------------------------------------------


def test_action_build_creates_cache(repo):
    """run_action_generate() builds a cache ref for the given commit.

    Assertions:
      - returncode == 0 (script exited cleanly)
      - generation block has mode in {"full", "delta"}
      - refs/agent-git-smart/<commit> now exists in the repo
      - bundle_bytes > 0 (the blobless bundle was written)
    """
    r, commit0 = repo
    # Add a second commit on master so there is something to delta against.
    new_commit = make_commit(
        r,
        commit0,
        {"generated_note.md": "# action build test\n"},
        branch="refs/heads/master",
    )

    result = run_action_generate(
        repo_dir=r.path.rstrip("/"),
        commit=new_commit,
        branch="master",
    )

    assert result["returncode"] == 0, (
        f"generate_agentgitsmart.py failed (rc={result['returncode']}): "
        f"{result.get('error')}"
    )

    gen = result["generation"]
    assert gen.get("mode") in {"full", "delta"}, (
        f"Expected generation mode 'full' or 'delta', got: {gen!r}"
    )

    # The cache side-ref must have been written.
    ref_name = f"refs/agent-git-smart/{new_commit}"
    assert ref_name in r.references, (
        f"Cache ref {ref_name!r} was not created by run_action_generate"
    )

    # The action path also writes a blobless bundle — that's the extra work
    # vs the in-process hook.
    assert result["bundle_bytes"] > 0, (
        f"Expected bundle_bytes > 0 (blobless bundle was not written): {result!r}"
    )


def test_action_build_result_structure(repo):
    """run_action_generate() always returns the required keys, even on success."""
    r, commit0 = repo
    result = run_action_generate(
        repo_dir=r.path.rstrip("/"),
        commit=commit0,
        branch="master",
    )
    for key in ("wall_s", "generation", "bundle_bytes", "returncode", "error"):
        assert key in result, f"Missing key {key!r} in result dict"
    assert isinstance(result["wall_s"], float)
    assert isinstance(result["bundle_bytes"], int)
    assert isinstance(result["generation"], dict)
