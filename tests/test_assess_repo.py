"""Tests for scripts/assess_repo.py — "is agentcache worth it for my repo?" analyzer.

These tests are CALIBRATED against the real 15-repo harness experiment
(testharness/data/experiments/6d89556c.json), whose measured verdicts come
from proxy-measured bytes — NOT from repo shape.  The fixtures below use the
ACTUAL static signals measured by gather_signals() on benchmark/repos/*.git.

Ground-truth measured verdicts (from render_experiment_report.suitability_verdict):
  agentcache worthwhile : anthropic-cookbook, anthropic-sdk-python, prettier
  blobless is enough    : fd, cpython, git, git-lfs
  worth it only at high reuse : ripgrep, bat, codex, django, go, redis, jq, ohmyzsh

The predictor is deliberately CONSERVATIVE (asymmetric safety: a false "adopt"
is worse than a false "skip").  The HARD constraint these tests lock in is:
predict_suitability NEVER returns "agentcache worthwhile" for a repo the harness
measured as "blobless is enough" (cpython, git, fd, git-lfs).

Coverage:
  - predict_suitability(): real calibration signal dicts + the safety regression.
  - gather_signals(): git plumbing against a tiny real repo built in tmp_path.
  - main() CLI: local-path exit-0 + label in stdout, --json key shape.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import under test (scripts/ is a namespace package; pythonpath=[".") )
# ---------------------------------------------------------------------------
from scripts.assess_repo import gather_signals, main, predict_suitability

# ---------------------------------------------------------------------------
# Label constants — must match predict_suitability return values exactly
# ---------------------------------------------------------------------------
_BLOBLESS = "blobless is enough"
_WORTHWHILE = "agentcache worthwhile"
_INCONCLUSIVE = "inconclusive — measure to be sure"

# Every label the STATIC predictor can emit.
_PREDICTOR_LABELS = {_BLOBLESS, _WORTHWHILE, _INCONCLUSIVE}

# ---------------------------------------------------------------------------
# REAL calibration signals — measured by gather_signals() on the local mirrors
# at benchmark/repos/<name>.git.  Keep these in sync with the calibration table.
#
# fields: file_count, total_bytes, asset_bytes, nonsource_asset_bytes,
#         source_file_count, history_depth
# ---------------------------------------------------------------------------
CALIBRATION: dict[str, dict] = {
    # measured: agentcache worthwhile — the clear, cookbook-like extreme
    "anthropic-cookbook": dict(
        file_count=574,
        total_bytes=208_460_094,
        asset_bytes=205_984_125,
        nonsource_asset_bytes=187_526_398,
        source_file_count=333,
        history_depth=588,
        measured=_WORTHWHILE,
        expected=_WORTHWHILE,  # asset 98.8%, nonsource 90.0% → clears both gates
    ),
    # measured: agentcache worthwhile — but statically LEAN (0.6% non-source);
    # static signals genuinely cannot see the win → safe UNDER-promise to blobless
    "prettier": dict(
        file_count=9373,
        total_bytes=23_442_891,
        asset_bytes=17_587_992,
        nonsource_asset_bytes=16_683_410,
        source_file_count=7119,
        history_depth=11_447,
        measured=_WORTHWHILE,
        expected=_INCONCLUSIVE,  # asset 75.0% < 85% gate → abstain (safe)
    ),
    # measured: blobless is enough — DANGEROUS historic false-positive (asset 52%)
    "cpython": dict(
        file_count=5801,
        total_bytes=135_191_118,
        asset_bytes=70_259_069,
        nonsource_asset_bytes=46_987_823,
        source_file_count=3958,
        history_depth=131_946,
        measured=_BLOBLESS,
        expected=_INCONCLUSIVE,  # nonsource 34.8% < 65% gate → abstain, NOT worthwhile
    ),
    # measured: blobless is enough — DANGEROUS historic false-positive (asset 53%)
    "git": dict(
        file_count=4764,
        total_bytes=47_605_412,
        asset_bytes=25_401_781,
        nonsource_asset_bytes=25_401_781,
        source_file_count=2379,
        history_depth=81_314,
        measured=_BLOBLESS,
        expected=_INCONCLUSIVE,  # nonsource 53.4% < 65% gate → abstain, NOT worthwhile
    ),
    # measured: blobless is enough — small repo (57 files)
    "fd": dict(
        file_count=57,
        total_bytes=562_376,
        asset_bytes=189_311,
        nonsource_asset_bytes=189_311,
        source_file_count=45,
        history_depth=1944,
        measured=_BLOBLESS,
        expected=_BLOBLESS,  # < 150 files
    ),
    # measured: blobless is enough — little genuine non-source payload (14.1%)
    "git-lfs": dict(
        file_count=650,
        total_bytes=3_245_683,
        asset_bytes=458_630,
        nonsource_asset_bytes=458_630,
        source_file_count=454,
        history_depth=9675,
        measured=_BLOBLESS,
        expected=_BLOBLESS,  # nonsource 14.1% < 15%
    ),
    # measured: worth it only at high reuse — messy middle → abstain
    "ripgrep": dict(
        file_count=222,
        total_bytes=3_117_897,
        asset_bytes=950_359,
        nonsource_asset_bytes=950_359,
        source_file_count=148,
        history_depth=2217,
        measured="worth it only at high reuse",
        expected=_INCONCLUSIVE,  # nonsource 30.5% → messy middle
    ),
}

# The four measured-"blobless is enough" repos — the HARD-constraint fixtures.
_BLOBLESS_FIXTURES = ("cpython", "git", "fd", "git-lfs")


def _predict(name: str) -> dict:
    """Run predict_suitability on the real calibration signals for *name*."""
    c = CALIBRATION[name]
    return predict_suitability(
        file_count=c["file_count"],
        total_bytes=c["total_bytes"],
        asset_bytes=c["asset_bytes"],
        nonsource_asset_bytes=c["nonsource_asset_bytes"],
        source_file_count=c["source_file_count"],
        history_depth=c["history_depth"],
    )


# ---------------------------------------------------------------------------
# Tiny-repo helpers for gather_signals / CLI integration tests
# ---------------------------------------------------------------------------

_LARGE_FILE_BYTES = 256 * 1024  # 256 KiB — the size threshold used by the impl


def _make_git_repo(tmp_path: Path) -> Path:
    """Create an initialised git repo in *tmp_path* and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _commit_all(repo: Path, message: str = "init") -> None:
    """Stage everything and create a commit."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
    )


# ===========================================================================
# predict_suitability — REAL calibration cases
# ===========================================================================


class TestCalibration:
    """Each case uses the actual measured static signals for a real repo."""

    @pytest.mark.parametrize("name", list(CALIBRATION.keys()))
    def test_predicted_label_matches_calibration(self, name: str) -> None:
        """predict_suitability reproduces the calibrated expected label."""
        result = _predict(name)
        assert result["label"] == CALIBRATION[name]["expected"], (
            f"{name}: expected {CALIBRATION[name]['expected']!r}, "
            f"got {result['label']!r}"
        )
        assert result["predicted"] is True

    def test_cookbook_is_worthwhile_the_clear_extreme(self) -> None:
        """anthropic-cookbook (asset 98.8%, nonsource 90%) → agentcache worthwhile."""
        result = _predict("anthropic-cookbook")
        assert result["label"] == _WORTHWHILE
        assert result["confidence"] == "high"

    def test_cpython_not_worthwhile_despite_high_asset_ratio(self) -> None:
        """cpython looked blob-heavy (asset 52%) but measured blobless → must abstain."""
        result = _predict("cpython")
        assert result["label"] != _WORTHWHILE
        assert result["label"] == _INCONCLUSIVE

    def test_git_not_worthwhile_despite_high_asset_ratio(self) -> None:
        """git looked blob-heavy (asset 53%) but measured blobless → must abstain."""
        result = _predict("git")
        assert result["label"] != _WORTHWHILE
        assert result["label"] == _INCONCLUSIVE


class TestSafetyConstraint:
    """The HARD constraint: never over-promise on a measured-blobless repo."""

    @pytest.mark.parametrize("name", _BLOBLESS_FIXTURES)
    def test_no_dangerous_false_positive(self, name: str) -> None:
        """predict_suitability must NEVER say 'agentcache worthwhile' for a repo
        the harness measured as 'blobless is enough' (cpython, git, fd, git-lfs).

        This is the single most important guarantee of the tool: a false 'adopt'
        is the worst possible error.
        """
        result = _predict(name)
        assert CALIBRATION[name]["measured"] == _BLOBLESS  # sanity
        assert result["label"] != _WORTHWHILE, (
            f"DANGEROUS FALSE POSITIVE: {name} is measured 'blobless is enough' "
            f"but predicted {result['label']!r}"
        )

    def test_zero_dangerous_false_positives_across_calibration_set(self) -> None:
        """Aggregate check across every measured-blobless fixture: count == 0."""
        dangerous = [
            name
            for name in _BLOBLESS_FIXTURES
            if _predict(name)["label"] == _WORTHWHILE
        ]
        assert dangerous == [], f"Dangerous false positives: {dangerous}"


class TestInconclusiveReachable:
    """The 4th, abstaining outcome must be reachable and low-confidence."""

    def test_inconclusive_label_is_reachable(self) -> None:
        """A messy-middle repo (high-ish but not extreme asset ratio) → inconclusive."""
        # cpython-like: asset 52%, nonsource 35% — neither skip nor clear win.
        result = predict_suitability(
            file_count=5801,
            total_bytes=135_191_118,
            asset_bytes=70_259_069,
            nonsource_asset_bytes=46_987_823,
            source_file_count=3958,
            history_depth=131_946,
        )
        assert result["label"] == _INCONCLUSIVE
        assert result["confidence"] == "low"

    def test_inconclusive_reason_states_asymmetry(self) -> None:
        """The abstaining reason must articulate 'false adopt worse than false skip'."""
        result = _predict("git")
        assert result["label"] == _INCONCLUSIVE
        reason = result["reason"].lower()
        assert "abstain" in reason or "measure" in reason
        assert "adopt" in reason  # names the asymmetric philosophy

    def test_inconclusive_confidence_is_low(self) -> None:
        result = _predict("prettier")
        assert result["label"] == _INCONCLUSIVE
        assert result["confidence"] == "low"


class TestWorthwhileGateGuards:
    """Both gates (asset ratio AND genuine non-source ratio) must be required."""

    def test_high_asset_ratio_from_large_source_does_not_trip_worthwhile(self) -> None:
        """asset_ratio 0.95 but nonsource_ratio ~0 (all large SOURCE files) → NOT worthwhile.

        This is the exact cpython/git failure mode the redesign fixes.
        """
        result = predict_suitability(
            file_count=2000,
            total_bytes=100_000_000,
            asset_bytes=95_000_000,  # 95% "asset" by the size rule…
            nonsource_asset_bytes=0,  # …but it's ALL large source files
            source_file_count=1500,
        )
        assert result["label"] != _WORTHWHILE
        assert result["label"] == _BLOBLESS  # nonsource_ratio 0 < 0.15

    def test_missing_nonsource_signal_cannot_reach_worthwhile(self) -> None:
        """If nonsource_asset_bytes is omitted, worthwhile is unreachable (safe default)."""
        result = predict_suitability(
            file_count=2000,
            total_bytes=100_000_000,
            asset_bytes=99_000_000,  # 99% assets by size rule
            source_file_count=200,
            # nonsource_asset_bytes omitted → treated as 0
        )
        assert result["label"] != _WORTHWHILE

    def test_both_gates_high_yields_worthwhile(self) -> None:
        """asset 90% AND nonsource 80% AND ≥10MiB AND ≥150 files → worthwhile."""
        result = predict_suitability(
            file_count=300,
            total_bytes=50_000_000,
            asset_bytes=45_000_000,  # 90%
            nonsource_asset_bytes=40_000_000,  # 80%
            source_file_count=250,
        )
        assert result["label"] == _WORTHWHILE

    def test_tiny_total_bytes_not_worthwhile_even_if_ratios_high(self) -> None:
        """Below the absolute-payload floor, high ratios still don't read worthwhile."""
        result = predict_suitability(
            file_count=300,
            total_bytes=1_000_000,  # 1 MiB < 10 MiB floor
            asset_bytes=990_000,
            nonsource_asset_bytes=950_000,
            source_file_count=200,
        )
        assert result["label"] != _WORTHWHILE


class TestPredictStructure:
    """Return-dict structure and guards."""

    def test_small_repo_is_blobless(self) -> None:
        result = predict_suitability(
            file_count=57,
            total_bytes=1_200_000,
            asset_bytes=50_000,
            nonsource_asset_bytes=50_000,
            source_file_count=55,
        )
        assert result["label"] == _BLOBLESS
        assert result["confidence"] == "high"

    def test_zero_bytes_no_crash(self) -> None:
        """total_bytes=0 guard: no divide-by-zero; yields blobless (small repo)."""
        result = predict_suitability(
            file_count=0,
            total_bytes=0,
            asset_bytes=0,
            nonsource_asset_bytes=0,
            source_file_count=0,
        )
        assert result["label"] == _BLOBLESS
        assert result["predicted"] is True

    def test_none_total_bytes_no_crash(self) -> None:
        """total_bytes=None must not crash (ratios treated as 0)."""
        result = predict_suitability(
            file_count=500,
            total_bytes=None,  # type: ignore[arg-type]
            asset_bytes=100_000,
            nonsource_asset_bytes=100_000,
            source_file_count=490,
        )
        # nonsource_ratio 0 → blobless
        assert result["label"] == _BLOBLESS

    def test_return_dict_has_required_keys(self) -> None:
        result = _predict("cpython")
        for key in ("label", "reason", "note", "signals", "confidence", "predicted"):
            assert key in result, f"Missing key: {key}"

    def test_signals_expose_both_ratios(self) -> None:
        result = _predict("cpython")
        sig = result["signals"]
        assert "asset_ratio" in sig
        assert "nonsource_ratio" in sig
        assert "nonsource_asset_bytes" in sig
        # cpython: nonsource 46_987_823 / 135_191_118 ≈ 0.3476
        assert abs(sig["nonsource_ratio"] - 0.3476) < 0.001

    def test_predicted_label_is_always_a_predictor_label(self) -> None:
        for name in CALIBRATION:
            assert _predict(name)["label"] in _PREDICTOR_LABELS

    # -- deep-history note (unchanged behaviour) ----------------------------

    def test_deep_history_note_present(self) -> None:
        """cpython's 131,946 commits (> 20,000) → deep-history note populated."""
        result = _predict("cpython")
        assert result.get("note"), "Expected a deep-history note"
        assert "deep" in result["note"].lower() or "131" in result["note"]

    def test_no_deep_history_note_when_shallow(self) -> None:
        """anthropic-cookbook's 588 commits → no deep-history note."""
        result = _predict("anthropic-cookbook")
        assert not result.get("note"), "Unexpected note for shallow history"

    def test_no_deep_history_note_when_none(self) -> None:
        result = predict_suitability(
            file_count=5000,
            total_bytes=50_000_000,
            asset_bytes=1_000_000,
            nonsource_asset_bytes=1_000_000,
            source_file_count=4900,
            history_depth=None,
        )
        assert not result.get("note")


# ===========================================================================
# gather_signals — integration test against a tiny real git repo
# ===========================================================================


class TestGatherSignals:
    """Integration: build a real git repo, call gather_signals(), check results."""

    @pytest.fixture()
    def tiny_repo(self, tmp_path: Path) -> Path:
        """A git repo with source files, a large *source* file, and a binary asset."""
        repo = _make_git_repo(tmp_path)

        # Source files
        (repo / "README.md").write_text("# Test repo\n")
        src = repo / "src"
        src.mkdir()
        (src / "main.py").write_text("def main(): pass\n")
        (src / "util.py").write_text("def util(): pass\n")

        # A small JSON (source-classified)
        (repo / "config.json").write_text('{"key": "value"}\n')

        # A LARGE source file (.py, > 256 KiB): "asset" by the size rule but NOT
        # a genuine non-source asset — must be EXCLUDED from nonsource_asset_bytes.
        big_src = repo / "generated.py"
        big_src.write_text("x = 1\n" * 60_000)  # well over 256 KiB

        # A binary asset (.bin, > 256 KiB): genuine non-source asset.
        binary = repo / "data" / "model.bin"
        binary.parent.mkdir()
        binary.write_bytes(b"\x00" * (_LARGE_FILE_BYTES + 1024))  # 257 KiB

        _commit_all(repo)
        return repo

    def test_file_count(self, tiny_repo: Path) -> None:
        signals = gather_signals(tiny_repo)
        # README.md, src/main.py, src/util.py, config.json, generated.py, data/model.bin = 6
        assert signals["file_count"] == 6

    def test_nonsource_excludes_large_source_file(self, tiny_repo: Path) -> None:
        """The big .py is in asset_bytes (size rule) but NOT nonsource_asset_bytes."""
        signals = gather_signals(tiny_repo)
        big_src_bytes = len("x = 1\n" * 60_000)
        # asset_bytes includes the large .py; nonsource_asset_bytes must not.
        assert signals["asset_bytes"] >= big_src_bytes
        assert (
            signals["nonsource_asset_bytes"] <= signals["asset_bytes"] - big_src_bytes
        )
        # The binary .bin (the only genuine non-source asset) is counted.
        assert signals["nonsource_asset_bytes"] >= _LARGE_FILE_BYTES

    def test_source_file_count(self, tiny_repo: Path) -> None:
        signals = gather_signals(tiny_repo)
        # source = source-ext AND not oversized: README.md, main.py, util.py, config.json = 4
        # generated.py is oversized → asset; model.bin → asset.
        assert signals["source_file_count"] == 4

    def test_total_bytes_positive(self, tiny_repo: Path) -> None:
        signals = gather_signals(tiny_repo)
        assert signals["total_bytes"] > 0
        assert signals["total_bytes"] >= signals["asset_bytes"]

    def test_history_depth_is_int_or_none(self, tiny_repo: Path) -> None:
        signals = gather_signals(tiny_repo)
        hd = signals.get("history_depth")
        assert hd is None or (isinstance(hd, int) and hd >= 1)

    def test_required_signal_keys(self, tiny_repo: Path) -> None:
        signals = gather_signals(tiny_repo)
        for key in (
            "file_count",
            "total_bytes",
            "asset_bytes",
            "nonsource_asset_bytes",
            "source_file_count",
        ):
            assert key in signals, f"Missing key: {key}"

    def test_signals_feed_predictor(self, tiny_repo: Path) -> None:
        """gather_signals output must drive predict_suitability without error."""
        signals = gather_signals(tiny_repo)
        result = predict_suitability(
            file_count=signals["file_count"],
            total_bytes=signals["total_bytes"],
            asset_bytes=signals["asset_bytes"],
            nonsource_asset_bytes=signals["nonsource_asset_bytes"],
            source_file_count=signals["source_file_count"],
            history_depth=signals.get("history_depth"),
        )
        assert result["label"] in _PREDICTOR_LABELS


# ===========================================================================
# CLI — main() integration tests
# ===========================================================================


class TestCLI:
    """Tests for the main(argv) CLI entry point."""

    @pytest.fixture()
    def cli_repo(self, tmp_path: Path) -> Path:
        repo = _make_git_repo(tmp_path)
        (repo / "README.md").write_text("# hello\n")
        for i in range(5):
            (repo / f"src_{i}.py").write_text(f"x = {i}\n")
        _commit_all(repo)
        return repo

    def test_local_path_exits_0(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo)])
        captured = capsys.readouterr()
        assert captured.out

    def test_local_path_prints_label(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo)])
        captured = capsys.readouterr()
        assert any(label in captured.out for label in _PREDICTOR_LABELS), (
            f"No verdict label found in output:\n{captured.out}"
        )

    def test_json_flag_emits_valid_json(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo), "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, dict)

    def test_json_flag_contains_label(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo), "--json"])
        data = json.loads(capsys.readouterr().out)
        assert "label" in data
        assert data["label"] in _PREDICTOR_LABELS

    def test_json_flag_contains_signals(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo), "--json"])
        data = json.loads(capsys.readouterr().out)
        assert "signals" in data
        assert "file_count" in data["signals"]
        assert "nonsource_ratio" in data["signals"]

    def test_human_output_has_footer(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo)])
        assert "STATIC" in capsys.readouterr().out.upper()

    def test_footer_says_measure_before_adopting(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-worthwhile result must steer the user to MEASURE before adopting."""
        main([str(cli_repo)])
        out = capsys.readouterr().out.lower()
        assert "measure" in out and "adopt" in out

    def test_human_output_has_signals_table(
        self, cli_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([str(cli_repo)])
        out = capsys.readouterr().out.lower()
        assert any(kw in out for kw in ("file", "bytes", "size", "asset"))

    def test_agentcache_repo_itself(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Smoke test: run the analyzer against the AgentCache repo itself."""
        repo_root = Path(__file__).parent.parent
        main([str(repo_root)])
        captured = capsys.readouterr()
        assert any(label in captured.out for label in _PREDICTOR_LABELS)
