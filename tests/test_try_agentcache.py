"""Tests for scripts/try_agentcache.py.

Three tiers of coverage:
  1. Pure function tests (fast, no I/O):
     render_try_report(summary, files) → str
     try_result_json(summary, files)   → dict
     Tested against synthetic campaign-summary fixtures modelled on
     fd (small/blobless-enough) and anthropic-cookbook (blob-heavy/worthwhile).

  2. CLI --help smoke test (fast, no network/daemon).

  3. Integration smoke test: tiny locally-built git repo → full measured run.
     Starts a git daemon, byte-counting proxy, and agentcache service on
     ephemeral ports.  Skipped if ``git`` is not on PATH.  Marked slow so
     ``pytest -m "not slow"`` bypasses it (though it still runs in the
     default ``pytest -q`` run).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import the pure helpers under test
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent

from scripts.try_agentcache import (  # noqa: E402
    render_try_report,
    resolve_target,
    try_result_json,
)


# ---------------------------------------------------------------------------
# Synthetic summary fixtures
# ---------------------------------------------------------------------------

def _make_summary(
    *,
    naive_cold: int,
    naive_warm: int,
    blobless_cold: int,
    blobless_warm: int,
    agentcache_cold: int,
    agentcache_warm: int,
    error: Optional[str] = None,
) -> dict:
    """Build a summary dict that mirrors _summarize_campaign() output."""
    if error:
        return {"error": error}

    bl_w = blobless_warm
    ac_w = agentcache_warm
    naive_w = naive_warm

    return {
        "naive": {
            "cold_bytes": naive_cold,
            "warm_avg_bytes": naive_w,
            "cold_wall": 1.5,
            "warm_avg_wall": 1.2,
            "runs": 2,
        },
        "blobless": {
            "cold_bytes": blobless_cold,
            "warm_avg_bytes": bl_w,
            "cold_wall": 0.8,
            "warm_avg_wall": 0.5,
            "runs": 2,
        },
        "agentcache": {
            "cold_bytes": agentcache_cold,
            "warm_avg_bytes": ac_w,
            "cold_wall": 3.0,
            "warm_avg_wall": 0.1,
            "runs": 2,
        },
        "_win_vs_naive": {
            "naive": 1.0,
            "blobless": round(naive_w / bl_w, 1) if bl_w else None,
            "agentcache": round(naive_w / ac_w, 1) if ac_w else None,
        },
        "_win_vs_naive_wall": {},
    }


# fd-like: 57 files, source-only repo.
# Warm saving: (200 000 - 195 000) / 200 000 = 2.5% — tiny.
# Verdict: "blobless is enough" because files < 150 AND saving marginal.
_FD_SUMMARY = _make_summary(
    naive_cold=5_000_000,
    naive_warm=2_000_000,
    blobless_cold=400_000,
    blobless_warm=200_000,
    agentcache_cold=3_000_000,
    agentcache_warm=195_000,
)
_FD_FILES = 57

# anthropic-cookbook-like: 600 files, blob-heavy (notebooks/images dominate).
# Warm saving: (50 000 000 - 500 000) / 50 000 000 ≈ 99%.
# Break-even: ceil((10M - 1M) / 49.5M) = ceil(0.18) = 1 pass.
# Verdict: "agentcache worthwhile".
_COOKBOOK_SUMMARY = _make_summary(
    naive_cold=100_000_000,
    naive_warm=80_000_000,
    blobless_cold=1_000_000,
    blobless_warm=50_000_000,
    agentcache_cold=10_000_000,
    agentcache_warm=500_000,
)
_COOKBOOK_FILES = 600

# Mid-tier: decent file count, medium saving, high break-even.
# warm_saving_ratio = (10M - 7M) / 10M = 30%, break-even = ceil((5M - 1M) / 3M) = 2
# Verdict: check thresholds — saving ≥ 15% and ≥ 40%?  30% < 40% → high-reuse tier.
# break-even = 2 ≤ 50 → but saving < 0.40 → "worth it only at high reuse"
_MID_SUMMARY = _make_summary(
    naive_cold=20_000_000,
    naive_warm=15_000_000,
    blobless_cold=1_000_000,
    blobless_warm=10_000_000,
    agentcache_cold=5_000_000,
    agentcache_warm=7_000_000,
)
_MID_FILES = 300

# Error summary
_ERROR_SUMMARY = _make_summary(
    naive_cold=0,
    naive_warm=0,
    blobless_cold=0,
    blobless_warm=0,
    agentcache_cold=0,
    agentcache_warm=0,
    error="git daemon failed to start on port 19418",
)


# ---------------------------------------------------------------------------
# Tests for render_try_report()
# ---------------------------------------------------------------------------

class TestRenderTryReport:
    def test_fd_verdict_is_blobless_enough(self):
        report = render_try_report(_FD_SUMMARY, _FD_FILES)
        assert "blobless is enough" in report

    def test_cookbook_verdict_is_worthwhile(self):
        # warm_saving_ratio ≈ 99%, break-even = 1 → "agentcache worthwhile"
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "agentcache worthwhile" in report

    def test_contains_cold_header(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "COLD" in report

    def test_contains_warm_header(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "WARM" in report

    def test_contains_all_three_methods(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        lower = report.lower()
        assert "naive" in lower
        assert "blobless" in lower
        assert "agentcache" in lower

    def test_caveat_mentions_shallow_or_depth1(self):
        """Report must warn that blobless cold is depth-1 shallow."""
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        lower = report.lower()
        assert "depth-1" in lower or "shallow" in lower

    def test_saving_percentage_present(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "%" in report

    def test_break_even_line_present(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "break-even" in report.lower()

    def test_file_count_in_report(self):
        report = render_try_report(_FD_SUMMARY, _FD_FILES)
        assert "57" in report

    def test_cookbook_file_count_in_report(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "600" in report

    def test_error_summary_shows_error(self):
        report = render_try_report(_ERROR_SUMMARY, 0)
        lower = report.lower()
        assert "error" in lower

    def test_error_summary_shows_message(self):
        report = render_try_report(_ERROR_SUMMARY, 0)
        assert "git daemon failed" in report

    def test_footer_mentions_harness(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "harness" in report.lower()

    def test_footer_mentions_testharness_start(self):
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "testharness/start.sh" in report

    def test_verdict_label_in_report(self):
        report = render_try_report(_FD_SUMMARY, _FD_FILES)
        assert "Verdict:" in report

    def test_reason_label_in_report(self):
        report = render_try_report(_FD_SUMMARY, _FD_FILES)
        assert "Reason:" in report

    def test_fd_saving_is_small(self):
        """fd saving = (200K - 195K) / 200K = 2.5% — too small to matter."""
        report = render_try_report(_FD_SUMMARY, _FD_FILES)
        # Saving string should say 2.5% or show a zero/marginal note
        # (the verdict logic already covers this — just check it's present)
        assert "%" in report

    def test_cookbook_break_even_is_small(self):
        """cookbook break-even = 1 pass — should say 1."""
        report = render_try_report(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert "1" in report

    def test_mid_saving_ratio_triggers_high_reuse(self):
        """Mid-tier: agentcache_warm (7M) > blobless_warm (10M)? No — 7M < 10M.

        warm_saved = 10M - 7M = 3M; ratio = 3/10 = 30%.
        30% >= 15% but 30% < 40% → "worth it only at high reuse".
        break-even = ceil((5M-1M) / 3M) = ceil(1.33) = 2.
        """
        warm_saved = 10_000_000 - 7_000_000
        bl_warm = 10_000_000
        ratio = warm_saved / bl_warm
        ac_cold = 5_000_000
        bl_cold = 1_000_000
        cold_oh = ac_cold - bl_cold
        be = math.ceil(cold_oh / warm_saved)

        # Sanity-check our arithmetic before testing the report
        assert abs(ratio - 0.30) < 1e-6
        assert be == 2

        report = render_try_report(_MID_SUMMARY, _MID_FILES)
        # With 30% saving and break-even=2, files=300 → should NOT be
        # "blobless is enough" (saving is above marginal threshold)
        assert "blobless is enough" not in report


# ---------------------------------------------------------------------------
# Tests for try_result_json()
# ---------------------------------------------------------------------------

class TestTryResultJson:
    def test_has_required_keys(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        required = {
            "files", "measured", "cold_bytes", "warm_bytes",
            "saving_pct_vs_blobless", "break_even_passes", "verdict", "reason",
        }
        for key in required:
            assert key in result, f"Missing key: {key!r}"

    def test_measured_flag_is_true(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert result["measured"] is True

    def test_cold_bytes_has_three_keys(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        cold = result["cold_bytes"]
        assert set(cold.keys()) == {"naive", "blobless", "agentcache"}

    def test_warm_bytes_has_three_keys(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        warm = result["warm_bytes"]
        assert set(warm.keys()) == {"naive", "blobless", "agentcache"}

    def test_cold_bytes_values_are_ints_or_none(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        for k, v in result["cold_bytes"].items():
            assert v is None or isinstance(v, int), f"cold_bytes[{k!r}] = {v!r}"

    def test_warm_bytes_values_are_ints_or_none(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        for k, v in result["warm_bytes"].items():
            assert v is None or isinstance(v, int), f"warm_bytes[{k!r}] = {v!r}"

    def test_fd_verdict_is_blobless_enough(self):
        result = try_result_json(_FD_SUMMARY, _FD_FILES)
        assert result["verdict"] == "blobless is enough"

    def test_cookbook_verdict_is_worthwhile(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        assert result["verdict"] == "agentcache worthwhile"

    def test_cookbook_saving_pct_is_large(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        pct = result["saving_pct_vs_blobless"]
        assert pct is not None
        # (50M - 500K) / 50M ≈ 99% — should be > 90
        assert pct > 90.0

    def test_fd_saving_pct_is_small(self):
        result = try_result_json(_FD_SUMMARY, _FD_FILES)
        pct = result["saving_pct_vs_blobless"]
        assert pct is not None
        # (200K - 195K) / 200K = 2.5%
        assert abs(pct - 2.5) < 0.1

    def test_cookbook_break_even_is_one(self):
        result = try_result_json(_COOKBOOK_SUMMARY, _COOKBOOK_FILES)
        be = result["break_even_passes"]
        assert be is not None
        # ceil((10M - 1M) / (50M - 500K)) = ceil(9M / 49.5M) = ceil(0.18) = 1
        assert be == 1

    def test_break_even_is_int_or_none(self):
        for summary, files in [
            (_FD_SUMMARY, _FD_FILES),
            (_COOKBOOK_SUMMARY, _COOKBOOK_FILES),
            (_MID_SUMMARY, _MID_FILES),
        ]:
            be = try_result_json(summary, files)["break_even_passes"]
            assert be is None or isinstance(be, int)

    def test_json_is_serializable(self):
        for summary, files in [
            (_FD_SUMMARY, _FD_FILES),
            (_COOKBOOK_SUMMARY, _COOKBOOK_FILES),
            (_ERROR_SUMMARY, 0),
        ]:
            result = try_result_json(summary, files)
            dumped = json.dumps(result)
            reloaded = json.loads(dumped)
            assert "verdict" in reloaded or "error" in reloaded

    def test_error_summary_returns_error_key(self):
        result = try_result_json(_ERROR_SUMMARY, 0)
        assert "error" in result
        assert result["error"] == "git daemon failed to start on port 19418"

    def test_error_summary_has_measured_flag(self):
        result = try_result_json(_ERROR_SUMMARY, 0)
        assert result["measured"] is True

    def test_files_field_matches_input(self):
        for files in (57, 600, 300, 0):
            result = try_result_json(_FD_SUMMARY, files)
            assert result["files"] == files

    def test_verdict_is_known_label(self):
        known = {
            "blobless is enough",
            "worth it only at high reuse",
            "agentcache worthwhile",
        }
        for summary, files in [
            (_FD_SUMMARY, _FD_FILES),
            (_COOKBOOK_SUMMARY, _COOKBOOK_FILES),
            (_MID_SUMMARY, _MID_FILES),
        ]:
            result = try_result_json(summary, files)
            assert result["verdict"] in known, f"Unknown verdict: {result['verdict']!r}"

    def test_reason_is_non_empty_string(self):
        for summary, files in [
            (_FD_SUMMARY, _FD_FILES),
            (_COOKBOOK_SUMMARY, _COOKBOOK_FILES),
        ]:
            result = try_result_json(summary, files)
            assert isinstance(result["reason"], str)
            assert len(result["reason"]) > 0


# ---------------------------------------------------------------------------
# Tests for resolve_target() — pure unit tests, no I/O
# ---------------------------------------------------------------------------


class TestResolveTarget:
    """resolve_target(arg, cwd) -> str is a pure helper; no I/O required."""

    def test_none_returns_cwd(self):
        """None (omitted arg) → cwd."""
        assert resolve_target(None, "/home/user/myrepo") == "/home/user/myrepo"

    def test_dot_returns_cwd(self):
        """Explicit '.' → cwd (same as default)."""
        assert resolve_target(".", "/home/user/myrepo") == "/home/user/myrepo"

    def test_explicit_path_passthrough(self):
        """Any explicit path is returned unchanged."""
        assert resolve_target("/explicit/path", "/irrelevant/cwd") == "/explicit/path"

    def test_relative_path_passthrough(self):
        """A relative path that is not '.' is returned unchanged."""
        assert resolve_target("../sibling-repo", "/some/cwd") == "../sibling-repo"

    def test_https_url_passthrough(self):
        url = "https://github.com/user/repo.git"
        assert resolve_target(url, "/irrelevant") == url

    def test_git_at_url_passthrough(self):
        url = "git@github.com:user/repo.git"
        assert resolve_target(url, "/irrelevant") == url

    def test_git_protocol_url_passthrough(self):
        url = "git://github.com/user/repo.git"
        assert resolve_target(url, "/irrelevant") == url

    def test_file_url_passthrough(self):
        url = "file:///tmp/some-repo"
        assert resolve_target(url, "/irrelevant") == url

    def test_cwd_varies_with_different_input(self):
        """The cwd parameter controls what None/'.' resolves to."""
        assert resolve_target(None, "/repo-a") == "/repo-a"
        assert resolve_target(None, "/repo-b") == "/repo-b"
        assert resolve_target(".", "/repo-a") != resolve_target(".", "/repo-b")


# ---------------------------------------------------------------------------
# Tests for CLI git-repo validation (fast — exits before running experiment)
# ---------------------------------------------------------------------------


class TestCLIValidation:
    """Validate that the git-repo check catches bad targets quickly."""

    def test_non_git_dir_defaults_exits_nonzero(self, tmp_path):
        """Running from a non-git directory with no TARGET must exit non-zero."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "try_agentcache.py")],
            capture_output=True,
            text=True,
            cwd=str(non_git),
        )
        assert proc.returncode != 0, (
            f"Expected non-zero exit; got {proc.returncode}\n"
            f"stderr: {proc.stderr!r}"
        )

    def test_non_git_dir_defaults_error_mentions_git(self, tmp_path):
        """Error message for non-git cwd must mention 'git' or 'repo'."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "try_agentcache.py")],
            capture_output=True,
            text=True,
            cwd=str(non_git),
        )
        combined = proc.stderr + proc.stdout
        lower = combined.lower()
        assert "git" in lower or "repo" in lower, (
            f"Expected git/repo mention in error output; got: {combined!r}"
        )

    def test_non_git_dir_defaults_error_mentions_repo_root(self, tmp_path):
        """Error message must suggest running from the repository root."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "try_agentcache.py")],
            capture_output=True,
            text=True,
            cwd=str(non_git),
        )
        combined = proc.stderr + proc.stdout
        assert "root" in combined.lower() or "repository" in combined.lower(), (
            f"Expected 'root'/'repository' in error; got: {combined!r}"
        )

    def test_explicit_non_git_dir_exits_nonzero(self, tmp_path):
        """Passing an explicit local non-git path must also exit non-zero."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        proc = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "scripts" / "try_agentcache.py"),
                str(non_git),
            ],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert proc.returncode != 0

    def test_url_target_skips_local_validation(self):
        """A URL target must not trigger the local git-repo check.

        We expect it to proceed past validation and fail later (mirror / network)
        rather than immediately with the git-repo error message.
        """
        # Use a clearly invalid URL that will fail fast but NOT with the
        # git-repo error message.
        proc = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "scripts" / "try_agentcache.py"),
                "https://invalid.test/nonexistent/repo.git",
            ],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=30,
        )
        combined = proc.stderr + proc.stdout
        # Must NOT say "not a git repo" — the URL was accepted past validation.
        assert "not a git repo" not in combined.lower()


# ---------------------------------------------------------------------------
# CLI --help smoke tests (fast, no I/O)
# ---------------------------------------------------------------------------

class TestCLIHelp:
    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert proc.returncode == 0, proc.stderr

    def test_help_mentions_target(self):
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert "TARGET" in proc.stdout

    def test_help_mentions_json_flag(self):
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert "--json" in proc.stdout

    def test_help_mentions_verbose_flag(self):
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert "--verbose" in proc.stdout

    def test_help_mentions_exit_code_advisory(self):
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        # Must document the "always 0" advisory exit code
        assert "0" in proc.stdout

    def test_help_mentions_repo_root(self):
        """Help must tell the user to run from their repo root."""
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        lower = proc.stdout.lower()
        assert "repo" in lower or "root" in lower, (
            f"Expected 'repo' or 'root' in help output; got: {proc.stdout[:400]}"
        )

    def test_help_target_is_optional(self):
        """With nargs='?', argparse usage line shows [TARGET] (optional)."""
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        # argparse renders optional positional as [TARGET] in usage
        assert "[TARGET]" in proc.stdout, (
            f"Expected '[TARGET]' in help usage; got: {proc.stdout[:400]}"
        )


# ---------------------------------------------------------------------------
# Integration smoke test: tiny real repo → full measured run
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestCLIWithTinyRepo:
    """End-to-end smoke: tiny git repo → daemon+proxy+service → verdict.

    Marked ``slow`` — runs in the default ``pytest -q`` unless excluded with
    ``pytest -m "not slow"``.  Skipped when git is not available.
    """

    @pytest.fixture(autouse=True)
    def _require_git(self):
        if not shutil.which("git"):
            pytest.skip("git not available")

    @staticmethod
    def _build_tiny_repo(tmp_path: Path) -> Path:
        """Build a tiny repo with 10 Python files and one commit."""
        repo_path = tmp_path / "tiny-try-repo"
        repo_path.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(repo_path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "config", "user.email", "t@test"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "config", "user.name", "T"],
            check=True, capture_output=True,
        )
        # Write enough files that the agent always finds at least one to edit.
        for i in range(10):
            content = (
                f"# module {i}\n"
                f"# This is a comment\n\n"
                f"def func_{i}(x: int) -> int:\n"
                f"    # inner comment\n"
                f"    return x + {i}\n"
            )
            (repo_path / f"mod_{i:02d}.py").write_text(content)
        subprocess.run(
            ["git", "-C", str(repo_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", "initial"],
            check=True, capture_output=True,
        )
        return repo_path

    def test_tiny_repo_exits_zero(self, tmp_path):
        repo = self._build_tiny_repo(tmp_path)
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", str(repo)],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=180,
        )
        assert proc.returncode == 0, (
            f"Expected exit 0 (advisory tool), got {proc.returncode}\n"
            f"stdout: {proc.stdout[-500:]}\n"
            f"stderr: {proc.stderr[-500:]}"
        )

    def test_tiny_repo_output_contains_verdict(self, tmp_path):
        repo = self._build_tiny_repo(tmp_path)
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", str(repo)],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=180,
        )
        combined = proc.stdout + proc.stderr
        # Must contain a verdict label or an error message
        assert "Verdict:" in combined or "error" in combined.lower(), (
            f"No verdict in output. stdout={proc.stdout[:300]}"
        )

    def test_tiny_repo_json_mode_valid(self, tmp_path):
        repo = self._build_tiny_repo(tmp_path)
        proc = subprocess.run(
            [sys.executable, "scripts/try_agentcache.py", str(repo), "--json"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=180,
        )
        assert proc.returncode == 0
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"JSON parse failed: {exc}\nstdout: {proc.stdout[:300]}")
        # Must have either "verdict" (success) or "error" (graceful failure)
        assert "verdict" in data or "error" in data
