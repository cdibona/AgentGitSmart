#!/usr/bin/env python3
"""assess_repo.py — "Is agentcache worth it for my repo?" analyzer.

A STATIC, cheap prediction from repo shape — NOT a measured experiment.
Points at a GitHub URL or local clone and returns an honest recommendation.

Usage:
    python scripts/assess_repo.py <path-or-url> [--json]

Exit code is always 0 (advisory tool).

Grounding (CALIBRATED against the real 15-repo harness experiment, whose
measured verdicts come from proxy-measured bytes, not repo shape):
  - agentcache's warm win is driven by BLOB-HEAVINESS: large *genuinely
    non-source* assets (notebooks, images, binaries, generated data) that
    blobless drags in but agentcache skips.
  - The naive static "asset_ratio" OVER-COUNTS: large source files and
    extensionless text/scripts inflate it, which made cpython and git LOOK
    blob-heavy when the harness measured them as "blobless is enough".  We fix
    this by separating GENUINE non-source bytes (by extension) from large
    source files, and only call a repo "worthwhile" at the cookbook-like
    extreme where BOTH the asset ratio AND the genuine non-source ratio are
    very high.

ASYMMETRIC SAFETY PHILOSOPHY (the whole point of this recalibration):
  A false "ADOPT" is far worse than a false "SKIP".  A pre-adoption tool that
  tells people to adopt when they shouldn't is the worst possible error.  So
  this predictor is deliberately CONSERVATIVE: when static signals can't
  confidently call it, it ABSTAINS ("inconclusive — measure to be sure")
  rather than over-promise.  HARD CONSTRAINT (validated on the 15-repo set):
  it NEVER returns "agentcache worthwhile" for a repo the harness measured as
  "blobless is enough" (cpython, fd, git, git-lfs).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# Threshold constants — tune here without touching logic
# ---------------------------------------------------------------------------

# Repos with fewer files than this are unlikely to benefit from agentcache.
# (fd has 57 files; the harness measured it "blobless is enough".)
_ASSESS_MIN_FILES: int = 150

# --- WORTHWHILE gate (deliberately a HIGH bar — "cookbook-like extreme" only) ---
# To emit "agentcache worthwhile" we require BOTH of these to hold, so that a
# high asset_ratio caused by large *source* files or extensionless text/scripts
# (the cpython / git false-positive failure mode) does NOT trip it:
#   1. asset_ratio          >= _ASSESS_WORTHWHILE_ASSET_RATIO   (extreme blob dominance)
#   2. nonsource_ratio       >= _ASSESS_WORTHWHILE_NONSOURCE_RATIO (the dominance is
#                                                                  GENUINE non-source bytes)
# Calibration: cookbook (asset 98.8%, nonsource 90.0%) PASSES; git (asset 53.4%,
# nonsource 53.4%) and cpython (asset 52.0%, nonsource 34.8%) FAIL both gates.
_ASSESS_WORTHWHILE_ASSET_RATIO: float = 0.85
_ASSESS_WORTHWHILE_NONSOURCE_RATIO: float = 0.65

# Worthwhile also requires a non-trivial absolute payload (cookbook is ~208 MiB);
# this stops a handful of small high-ratio config repos from reading "worthwhile".
_ASSESS_WORTHWHILE_MIN_TOTAL_BYTES: int = 10 * 1024 * 1024  # 10 MiB

# nonsource_ratio below this → so little genuine non-source payload that blobless
# already fetches almost nothing extra → "blobless is enough".
# Calibration: git-lfs (14.1%), codex (5.3%), anthropic-sdk-python (0.6%) land here.
_ASSESS_LOW_NONSOURCE_RATIO: float = 0.15

# Files larger than this (bytes) are classified as ASSET regardless of extension.
_ASSESS_LARGE_FILE_BYTES: int = 256 * 1024  # 256 KiB

# history_depth above this triggers a deep-history cold-cost NOTE.
_ASSESS_DEEP_HISTORY: int = 20_000

# ---------------------------------------------------------------------------
# Source-file extension set
# (everything else → ASSET; also any file > _ASSESS_LARGE_FILE_BYTES → ASSET)
# ---------------------------------------------------------------------------

_SOURCE_EXTS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".cc",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".java",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
        ".lua",
        ".php",
        ".cs",
        ".swift",
        ".kt",
        ".scala",
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".html",
        ".htm",
        ".css",
        ".xml",
        ".sql",
        ".tf",
        ".ini",
        ".cfg",
        ".conf",
        ".lock",
        ".gradle",
        ".cmake",
        ".makefile",
        ".mk",
        ".r",
        ".m",  # MATLAB / Objective-C header
        ".dart",
        ".elm",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".ml",
        ".mli",
        ".pl",
        ".pm",
        ".clj",
        ".cljs",
        ".cljc",
        ".groovy",
        ".jl",
        ".nim",
        ".v",
        ".zig",
    }
)

# ---------------------------------------------------------------------------
# Label constants
# ---------------------------------------------------------------------------

_LABEL_WORTHWHILE = "agentcache worthwhile"
_LABEL_BLOBLESS = "blobless is enough"
# New 4th, ABSTAINING outcome for the messy middle where static signals can't
# confidently call it.  Emitting this (rather than guessing "worthwhile") is how
# the predictor honours the asymmetric-safety philosophy.
_LABEL_INCONCLUSIVE = "inconclusive — measure to be sure"
# NOTE: "worth it only at high reuse" is a verdict the *measured* harness emits
# (render_experiment_report.suitability_verdict).  The STATIC predictor cannot
# safely separate the high-reuse cluster from the blobless cluster (cpython/git
# sit right inside it), so it abstains instead of claiming it.  Kept here only
# for reference / cross-tool label parity.
_LABEL_HIGH_REUSE = "worth it only at high reuse"

# ---------------------------------------------------------------------------
# Pure scoring core
# ---------------------------------------------------------------------------


def predict_suitability(
    *,
    file_count: int,
    total_bytes: "int | None",
    asset_bytes: int,
    source_file_count: int,
    history_depth: "int | None" = None,
    nonsource_asset_bytes: "int | None" = None,
) -> dict:
    """Predict whether agentcache is worth deploying for a repo.

    This is a STATIC, CONSERVATIVE heuristic based on repo shape — not a
    measured result.  It is deliberately asymmetric: a false "ADOPT" is treated
    as far worse than a false "SKIP", so when the static signals can't
    confidently call it the predictor ABSTAINS ("inconclusive — measure to be
    sure") rather than over-promise.

    HARD SAFETY CONSTRAINT (validated against the real 15-repo harness): this
    function NEVER returns "agentcache worthwhile" for a repo the harness
    measured as "blobless is enough".  It achieves that by requiring the
    worthwhile verdict to clear a HIGH bar on BOTH the overall asset ratio AND
    the *genuine* non-source ratio — so a high asset_ratio caused by large
    source files or extensionless text/scripts (the cpython / git false-positive
    failure mode) does not trip it.

    Args:
        file_count:            Total number of files committed at HEAD.
        total_bytes:           Sum of all blob sizes in bytes (None → 0).
        asset_bytes:           Sum of blob sizes classified as ASSET (non-source
                               extension OR oversized files).
        source_file_count:     Number of source-classified files.
        history_depth:         Number of commits in history (None if unknown).
        nonsource_asset_bytes: Sum of blob sizes whose *extension* is non-source
                               (i.e. genuine binary/data assets, EXCLUDING large
                               source files).  None → treated as 0, which makes
                               the worthwhile gate unreachable (the safe default:
                               without this signal we can't confirm the asset
                               dominance is real, so we never claim "worthwhile").

    Returns:
        dict with keys:
          label      — "agentcache worthwhile" | "blobless is enough" |
                       "inconclusive — measure to be sure"
          reason     — one-line human-readable explanation (states the asymmetry)
          note       — optional cold-cost warning for deep-history repos
          signals    — {file_count, total_bytes, asset_bytes, asset_ratio,
                        nonsource_asset_bytes, nonsource_ratio,
                        source_file_count, history_depth}
          confidence — "low" | "medium" | "high"
          predicted  — True (marks this as a static prediction)
    """
    # Guard: treat missing/zero total_bytes as 0-ratio
    _total = total_bytes if (total_bytes is not None and total_bytes > 0) else 0
    asset_ratio: float = asset_bytes / _total if _total > 0 else 0.0

    # Genuine non-source bytes (by extension).  Unknown → 0 (the SAFE default:
    # the worthwhile gate cannot be reached without confirmed non-source mass).
    _nonsrc = nonsource_asset_bytes if nonsource_asset_bytes is not None else 0
    nonsource_ratio: float = _nonsrc / _total if _total > 0 else 0.0

    # -----------------------------------------------------------------------
    # Decision rules — first match wins.  Ordering encodes the asymmetry:
    # the two "skip-ish" outcomes (small, low-payload) are checked first; the
    # worthwhile outcome is gated hard; everything else ABSTAINS.
    # -----------------------------------------------------------------------
    label: str
    reason: str
    confidence: str

    if file_count < _ASSESS_MIN_FILES:
        label = _LABEL_BLOBLESS
        reason = (
            f"small repo (~{file_count} files) — blobless's shallow clone is "
            "already cheap; agentcache's overhead isn't justified"
        )
        confidence = "high"

    elif nonsource_ratio < _ASSESS_LOW_NONSOURCE_RATIO:
        pct = round(nonsource_ratio * 100, 1)
        label = _LABEL_BLOBLESS
        reason = (
            f"little genuine non-source payload (~{pct}% of bytes) — blobless "
            "already fetches almost nothing extra; agentcache's warm edge is "
            "marginal"
        )
        confidence = "medium"

    elif (
        asset_ratio >= _ASSESS_WORTHWHILE_ASSET_RATIO
        and nonsource_ratio >= _ASSESS_WORTHWHILE_NONSOURCE_RATIO
        and _total >= _ASSESS_WORTHWHILE_MIN_TOTAL_BYTES
    ):
        apct = round(asset_ratio * 100, 1)
        npct = round(nonsource_ratio * 100, 1)
        label = _LABEL_WORTHWHILE
        reason = (
            f"blob-dominated at the extreme (~{apct}% assets, ~{npct}% genuine "
            "non-source) — agentcache skips what naive/blobless drag in; this "
            "is the clear, cookbook-like case where adopting is safe to "
            "recommend"
        )
        confidence = "high"

    else:
        apct = round(asset_ratio * 100, 1)
        npct = round(nonsource_ratio * 100, 1)
        label = _LABEL_INCONCLUSIVE
        reason = (
            f"messy middle (~{apct}% assets, ~{npct}% genuine non-source) — "
            "static signals can't confidently separate this from a "
            "blobless-is-enough repo. A false 'adopt' is worse than a false "
            "'skip', so this tool abstains: run the harness to measure before "
            "adopting"
        )
        confidence = "low"

    # -----------------------------------------------------------------------
    # Optional deep-history NOTE (added regardless of verdict)
    # -----------------------------------------------------------------------
    note: str = ""
    if history_depth is not None and history_depth > _ASSESS_DEEP_HISTORY:
        note = (
            f"deep history (~{history_depth:,} commits) → the per-commit "
            "bootstrap bundle is large; ensure it's CDN-cached"
        )

    return {
        "label": label,
        "reason": reason,
        "note": note,
        "signals": {
            "file_count": file_count,
            "total_bytes": total_bytes,
            "asset_bytes": asset_bytes,
            "asset_ratio": asset_ratio,
            "nonsource_asset_bytes": nonsource_asset_bytes,
            "nonsource_ratio": nonsource_ratio,
            "source_file_count": source_file_count,
            "history_depth": history_depth,
        },
        "confidence": confidence,
        "predicted": True,
    }


# ---------------------------------------------------------------------------
# Signal gathering from a real git repo
# ---------------------------------------------------------------------------


def _is_source_ext(path: str) -> bool:
    """True if *path*'s extension is in the source-extension set."""
    return PurePosixPath(path).suffix.lower() in _SOURCE_EXTS


def _classify_path(path: str, size: int) -> str:
    """Return "source" or "asset" for a file path + size.

    Classification rules (first match):
    1. size > _ASSESS_LARGE_FILE_BYTES → "asset" (regardless of extension)
    2. extension in _SOURCE_EXTS → "source"
    3. else → "asset"

    NOTE: this size-based rule is intentionally distinct from "genuine
    non-source" (see gather_signals' nonsource_asset_bytes), which is purely
    extension-based.  A large *source* file is an "asset" here but NOT a
    genuine non-source asset — that distinction is what stops cpython/git from
    reading as "worthwhile".
    """
    if size > _ASSESS_LARGE_FILE_BYTES:
        return "asset"
    return "source" if _is_source_ext(path) else "asset"


def gather_signals(repo_path: "str | Path") -> dict:
    """Gather repo-shape signals from a local git repository.

    Uses cheap git plumbing — no full content reads.

    ``git ls-tree -r -l HEAD`` is the workhorse: it enumerates every file
    in HEAD with its stored blob size.  Mode, type, OID, and size are parsed
    from each line; paths are classified as SOURCE vs ASSET.

    History depth comes from ``git rev-list --count HEAD`` with a guard for
    shallow clones (returns None on failure).

    Args:
        repo_path: Path to a local git repository (bare or non-bare).

    Returns:
        dict with keys: file_count, total_bytes, asset_bytes,
        nonsource_asset_bytes, source_file_count, history_depth.

    ``nonsource_asset_bytes`` counts bytes of files whose *extension* is
    non-source — the GENUINE binary/data payload — and deliberately EXCLUDES
    large source files (which inflate the size-based ``asset_bytes``).  This is
    the signal that keeps cpython/git from reading as "worthwhile".
    """
    repo_path = str(repo_path)

    # ------------------------------------------------------------------
    # ls-tree -r -l HEAD
    # Output per line (with -l flag):
    #   <mode> SP <type> SP <oid> SP <size> TAB <path>
    # size is "-" for tree entries (shouldn't appear with -r, but guard anyway)
    # ------------------------------------------------------------------
    result = subprocess.run(
        ["git", "-C", repo_path, "ls-tree", "-r", "-l", "HEAD"],
        capture_output=True,
        text=True,
    )
    # If HEAD doesn't exist (empty repo), return zero signals
    if result.returncode != 0:
        return {
            "file_count": 0,
            "total_bytes": 0,
            "asset_bytes": 0,
            "nonsource_asset_bytes": 0,
            "source_file_count": 0,
            "history_depth": None,
        }

    file_count = 0
    total_bytes = 0
    asset_bytes = 0
    nonsource_asset_bytes = 0
    source_file_count = 0

    for raw_line in result.stdout.splitlines():
        # Split on TAB first to isolate the path
        if "\t" not in raw_line:
            continue
        meta, path = raw_line.split("\t", 1)
        parts = meta.split()
        if len(parts) < 4:
            continue
        # parts: [mode, type, oid, size]
        size_str = parts[3]
        if size_str == "-":
            continue  # submodule / tree entry without blob size
        try:
            size = int(size_str)
        except ValueError:
            continue

        file_count += 1
        total_bytes += size

        kind = _classify_path(path, size)
        if kind == "asset":
            asset_bytes += size
        else:
            source_file_count += 1

        # GENUINE non-source bytes: extension-only test, ignoring the size rule.
        # A large source file (e.g. cpython's Misc/svnmap.txt is .txt = source)
        # does NOT count here, even though it is an "asset" by the size rule.
        if not _is_source_ext(path):
            nonsource_asset_bytes += size

    # ------------------------------------------------------------------
    # History depth — guard shallow clones
    # ------------------------------------------------------------------
    history_depth: "int | None" = None
    depth_result = subprocess.run(
        ["git", "-C", repo_path, "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    if depth_result.returncode == 0:
        try:
            history_depth = int(depth_result.stdout.strip())
        except ValueError:
            history_depth = None

    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "asset_bytes": asset_bytes,
        "nonsource_asset_bytes": nonsource_asset_bytes,
        "source_file_count": source_file_count,
        "history_depth": history_depth,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_bytes(n: "int | None") -> str:
    """Human-readable byte count."""
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n:,} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MiB"
    return f"{n / (1024 * 1024 * 1024):.2f} GiB"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_FOOTER = (
    "\nThis is a STATIC prediction from repo shape, not a measured result.\n"
    "It is deliberately CONSERVATIVE: a false 'adopt' is worse than a false\n"
    "'skip', so anything short of 'agentcache worthwhile' (i.e. 'blobless is\n"
    "enough' or 'inconclusive — measure to be sure') means: do NOT adopt yet —\n"
    "run the harness to MEASURE before adopting.\n"
    "  see experiments/ and scripts/render_experiment_report.py\n"
)


def _print_human_report(result: dict, signals: dict) -> None:
    """Print a clear human-readable report to stdout."""
    label: str = result["label"]
    reason: str = result["reason"]
    note: str = result.get("note", "")
    confidence: str = result["confidence"]

    asset_ratio: float = signals.get("asset_ratio", 0.0)

    print()
    print("=" * 60)
    print(f"  Verdict: {label}")
    print("=" * 60)
    print(f"  Reason:  {reason}")
    if note:
        print(f"  Note:    {note}")
    print(f"  Confidence: {confidence}")
    print()
    print("  Signals:")
    print(f"    Files in tree:    {signals['file_count']:,}")
    print(f"    Total size:       {_fmt_bytes(signals['total_bytes'])}")
    print(
        f"    Asset bytes:      {_fmt_bytes(signals['asset_bytes'])} ({_fmt_pct(asset_ratio)})"
    )
    print(f"    Source files:     {signals['source_file_count']:,}")
    hd = signals.get("history_depth")
    if hd is not None:
        print(f"    History depth:    {hd:,} commits")
    else:
        print("    History depth:    unknown (shallow clone or no history)")
    print(_FOOTER)


def main(argv: "list[str] | None" = None) -> None:
    """CLI entry point.

    Usage: assess_repo.py <path-or-url> [--json]

    TARGET may be:
      - A local directory path → analyse directly.
      - A git URL           → shallow-clone to a tempdir, analyse, clean up.

    Exit code is always 0 (advisory; never blocks anything).
    """
    if argv is None:
        argv = sys.argv[1:]

    # Simple arg parsing (stdlib only)
    emit_json = "--json" in argv
    positional = [a for a in argv if not a.startswith("--")]

    if not positional:
        print(
            "Usage: python scripts/assess_repo.py <path-or-url> [--json]",
            file=sys.stderr,
        )
        return

    target = positional[0]
    tmp_dir: "str | None" = None

    try:
        # ---------------------------------------------------------------
        # Resolve target to a local path
        # ---------------------------------------------------------------
        if os.path.isdir(target):
            repo_path = target
        else:
            # Treat as URL — shallow full clone for current-tree blob sizes.
            # (blobless clone would lazy-fetch blob sizes on ls-tree -l, so we
            # do a depth-1 full clone instead.)
            tmp_dir = tempfile.mkdtemp(prefix="assess_repo_")
            clone_cmd = [
                "git",
                "clone",
                "--depth=1",
                "--quiet",
                target,
                tmp_dir,
            ]
            clone_result = subprocess.run(clone_cmd, capture_output=True, text=True)
            if clone_result.returncode != 0:
                print(
                    f"Error: git clone failed:\n{clone_result.stderr}",
                    file=sys.stderr,
                )
                return
            repo_path = tmp_dir

        # ---------------------------------------------------------------
        # Gather signals
        # ---------------------------------------------------------------
        signals = gather_signals(repo_path)

        # ---------------------------------------------------------------
        # Predict suitability
        # ---------------------------------------------------------------
        result = predict_suitability(
            file_count=signals["file_count"],
            total_bytes=signals["total_bytes"],
            asset_bytes=signals["asset_bytes"],
            source_file_count=signals["source_file_count"],
            history_depth=signals.get("history_depth"),
        )

        # ---------------------------------------------------------------
        # Output
        # ---------------------------------------------------------------
        if emit_json:
            output = {
                "label": result["label"],
                "reason": result["reason"],
                "note": result.get("note", ""),
                "confidence": result["confidence"],
                "predicted": True,
                "signals": result["signals"],
                "target": target,
            }
            print(json.dumps(output, indent=2))
        else:
            _print_human_report(result, result["signals"])

    finally:
        if tmp_dir is not None and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
