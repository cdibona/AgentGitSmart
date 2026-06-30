#!/usr/bin/env python3
"""try_agentcache.py — "Would AgentCache help MY repo, and by how much?"

A one-shot MEASURED companion to scripts/assess_repo.py.
Where assess_repo predicts from repo shape, this tool MEASURES real network
bytes by running your repo through the testharness byte-counting proxy:
  - Pass 1 (COLD):  builds / seeds the agentcache artifacts.
  - Pass 2 (WARM):  steady-state per-agent cost.

Reports cold bytes for all three approaches, warm bytes, the agentcache
saving vs blobless, break-even passes, and a suitability verdict — all
backed by real measured numbers (not static prediction).

Usage:
    # Run from your repo root — TARGET defaults to the current directory:
    python scripts/try_agentcache.py [TARGET] [--json] [--verbose]

    # Explicit path or URL:
    python scripts/try_agentcache.py /path/to/repo
    python scripts/try_agentcache.py https://github.com/user/repo.git

TARGET may be a local repo path or any git URL.  If omitted the current
working directory is used (must be a git repository).
Exit code: always 0 (advisory).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

# Ensure the repo root is importable whether we are run as a script or as a
# module (e.g. from tests/).
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.render_experiment_report import (  # noqa: E402
    _fmt_bytes,
    suitability_verdict,
)
from testharness.experiment_runner import ExperimentRunner  # noqa: E402
from testharness.processes import AgentCacheService, GitDaemon  # noqa: E402
from testharness.proxy import ByteCountingProxy  # noqa: E402


# ---------------------------------------------------------------------------
# Target resolution and validation (pure / near-pure helpers)
# ---------------------------------------------------------------------------

_URL_PREFIXES = (
    "https://",
    "http://",
    "git@",
    "git://",
    "ssh://",
    "file://",
)


def resolve_target(arg: Optional[str], cwd: str) -> str:
    """Resolve the raw TARGET argument to a concrete path or URL.

    This is a pure function: no I/O, no side effects.

    Rules:
      - ``arg`` is ``None`` or ``"."``  →  return ``cwd``
      - ``arg`` is any other string     →  return ``arg`` unchanged

    Args:
        arg: The raw value parsed from the command-line positional, or
             ``None`` when the argument was omitted.
        cwd: The current working directory (``os.getcwd()``).

    Returns:
        The resolved target string (a filesystem path or git URL).

    Examples:
        >>> resolve_target(None, "/home/user/myrepo")
        '/home/user/myrepo'
        >>> resolve_target(".", "/home/user/myrepo")
        '/home/user/myrepo'
        >>> resolve_target("/explicit/path", "/irrelevant")
        '/explicit/path'
        >>> resolve_target("https://github.com/u/r.git", "/irrelevant")
        'https://github.com/u/r.git'
    """
    if arg is None or arg == ".":
        return cwd
    return arg


def _is_local_target(target: str) -> bool:
    """Return True if *target* looks like a local filesystem path (not a URL)."""
    return not any(target.startswith(p) for p in _URL_PREFIXES)


def _validate_local_git_repo(target: str) -> Optional[str]:
    """Return an error message if *target* is not a git repo, else ``None``.

    Runs ``git -C <target> rev-parse --git-dir`` (fast, no I/O beyond that).
    """
    result = subprocess.run(
        ["git", "-C", target, "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (
            f"not a git repo: {target!r} — "
            "run this from your repository's root, or pass a repo path/URL"
        )
    return None


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the OS for a free ephemeral port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _repo_name_from_target(target: str) -> str:
    """Extract a short repo name from a local path or git URL."""
    t = target.rstrip("/")
    name = Path(t).name
    # Strip .git suffix
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repo"


def _mirror_repo(target: str, repos_dir: str) -> tuple[str, str]:
    """Mirror *target* into *repos_dir*/<name>.git as a bare repo.

    Returns (name, bare_path).
    Raises RuntimeError if the clone fails.
    """
    name = _repo_name_from_target(target)
    bare_path = os.path.join(repos_dir, f"{name}.git")

    result = subprocess.run(
        ["git", "clone", "--mirror", target, bare_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone --mirror failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )

    # Enable blobless + agentcache filter support (required for the blobless
    # and agentcache approaches; see tests/test_bundle_and_coldstart.py).
    for key, val in [
        ("uploadpack.allowFilter", "true"),
        ("uploadpack.allowAnySHA1InWant", "true"),
    ]:
        subprocess.run(
            ["git", "--git-dir", bare_path, "config", key, val],
            capture_output=True,
        )

    return name, bare_path


# ---------------------------------------------------------------------------
# Pure report-rendering functions — unit-testable with no I/O
# ---------------------------------------------------------------------------


def render_try_report(summary: dict, files: int) -> str:
    """Render a human-readable try-agentcache measurement report.

    This is a pure function: no I/O, no side effects.

    Args:
        summary: Per-method measurement dict as produced by
                 ``ExperimentRunner._summarize_campaign()``.  Expected keys:
                 ``"naive"``, ``"blobless"``, ``"agentcache"``, each with at
                 least ``cold_bytes`` and ``warm_avg_bytes``; and
                 ``"_win_vs_naive"``.  An ``"error"`` key at the top level
                 means the campaign failed.
        files:   File count in the repo HEAD tree (for verdict + header).

    Returns:
        Multi-line human-readable string (no trailing newline).
    """
    error = summary.get("error")
    if error:
        lines = [
            "=" * 64,
            "  AgentCache trial measurement",
            "=" * 64,
            f"  ERROR: {error}",
            "=" * 64,
        ]
        return "\n".join(lines)

    naive = summary.get("naive") or {}
    blobless = summary.get("blobless") or {}
    agentcache = summary.get("agentcache") or {}

    naive_cold: Optional[int] = naive.get("cold_bytes")
    naive_warm: Optional[int] = naive.get("warm_avg_bytes")
    bl_cold: Optional[int] = blobless.get("cold_bytes")
    bl_warm: Optional[int] = blobless.get("warm_avg_bytes")
    ac_cold: Optional[int] = agentcache.get("cold_bytes")
    ac_warm: Optional[int] = agentcache.get("warm_avg_bytes")

    # Warm saving vs blobless (the honest competitor, not naive)
    warm_saved: Optional[float] = None
    warm_saving_ratio: Optional[float] = None
    if bl_warm is not None and ac_warm is not None:
        warm_saved = bl_warm - ac_warm
        if bl_warm > 0:
            warm_saving_ratio = warm_saved / bl_warm

    # Cold overhead and break-even
    cold_overhead: Optional[float] = None
    break_even: Optional[int] = None
    if bl_cold is not None and ac_cold is not None:
        cold_overhead = ac_cold - bl_cold
        if warm_saved is not None and warm_saved > 0:
            if cold_overhead is None or cold_overhead <= 0:
                break_even = 0
            else:
                break_even = math.ceil(cold_overhead / warm_saved)

    # Suitability verdict
    label, reason = suitability_verdict(
        files=files,
        warm_saving_ratio=warm_saving_ratio,
        break_even_passes=break_even,
    )

    # Saving % display
    if warm_saving_ratio is not None and warm_saving_ratio > 0:
        saving_str = f"{warm_saving_ratio * 100:.1f}% vs blobless"
    elif warm_saving_ratio is not None:
        saving_str = "0% — agentcache is NOT cheaper than blobless here"
    else:
        saving_str = "— (data unavailable)"

    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"  AgentCache trial: {files:,} files in repo")
    lines.append("  REAL measured network bytes via byte-counting proxy.")
    lines.append("  Pass 1 = COLD (builds artifacts)  |  Pass 2 = WARM (steady state)")
    lines.append("  Simulated agent edits 2% of source files per pass.")
    lines.append("=" * 64)
    lines.append("")

    # COLD section
    lines.append("  COLD pass (one-time cost to seed agentcache artifacts):")
    lines.append(f"    naive:      {_fmt_bytes(naive_cold)}")
    lines.append(
        f"    blobless:   {_fmt_bytes(bl_cold)}  ← depth-1 shallow (no history)"
    )
    lines.append(
        f"    agentcache: {_fmt_bytes(ac_cold)}  ← full-history blobless clone"
    )
    lines.append("")
    lines.append(
        "  ⚠ blobless cold ≠ agentcache cold: different products.  blobless cold"
    )
    lines.append("    is depth-1 shallow (no history). agentcache cold delivers full")
    lines.append("    history. In production the bootstrap bundle is built once per")
    lines.append("    commit and CDN-cached — a per-commit cost, not per-agent.")
    lines.append("")

    # WARM section
    lines.append("  WARM per-agent-pass (steady-state after artifacts are seeded):")
    lines.append(f"    naive:      {_fmt_bytes(naive_warm)}")
    lines.append(f"    blobless:   {_fmt_bytes(bl_warm)}")
    lines.append(f"    agentcache: {_fmt_bytes(ac_warm)}")
    lines.append(f"    saving:     {saving_str}")
    if break_even is not None:
        if break_even == 0:
            lines.append("    break-even: immediate (agentcache cold ≤ blobless cold)")
        else:
            lines.append(
                f"    break-even: ~{break_even} warm agent pass(es) vs blobless"
            )
    else:
        lines.append("    break-even: — (no warm byte win over blobless)")
    lines.append("")

    # Verdict
    bar = "  " + "─" * 60
    lines.append(bar)
    lines.append(f"  Verdict:  {label}")
    lines.append(f"  Reason:   {reason}")
    lines.append(bar)
    lines.append("")
    lines.append(
        "  This measured your repo with a simulated agent editing 2% of files."
    )
    lines.append(
        "  For deeper analysis (more passes, human commits, latency, multi-repo)"
    )
    lines.append("  run the full harness: bash testharness/start.sh")

    return "\n".join(lines)


def try_result_json(summary: dict, files: int) -> dict:
    """Return a machine-readable JSON-serializable dict for try_agentcache.

    This is a pure function: no I/O, no side effects.

    Args:
        summary: Per-method measurement dict (same shape as for
                 ``render_try_report``).
        files:   File count in the repo HEAD tree.

    Returns:
        JSON-serializable dict with keys:
        ``files``, ``measured``, ``cold_bytes``, ``warm_bytes``,
        ``saving_pct_vs_blobless``, ``break_even_passes``,
        ``verdict``, ``reason``.
        On error: ``error`` key instead of byte data.
    """
    error = summary.get("error")
    if error:
        return {"files": files, "measured": True, "error": error}

    naive = summary.get("naive") or {}
    blobless = summary.get("blobless") or {}
    agentcache = summary.get("agentcache") or {}

    naive_cold: Optional[int] = naive.get("cold_bytes")
    naive_warm: Optional[int] = naive.get("warm_avg_bytes")
    bl_cold: Optional[int] = blobless.get("cold_bytes")
    bl_warm: Optional[int] = blobless.get("warm_avg_bytes")
    ac_cold: Optional[int] = agentcache.get("cold_bytes")
    ac_warm: Optional[int] = agentcache.get("warm_avg_bytes")

    warm_saved: Optional[float] = None
    warm_saving_ratio: Optional[float] = None
    if bl_warm is not None and ac_warm is not None:
        warm_saved = bl_warm - ac_warm
        if bl_warm > 0:
            warm_saving_ratio = warm_saved / bl_warm

    cold_overhead: Optional[float] = None
    break_even: Optional[int] = None
    if bl_cold is not None and ac_cold is not None:
        cold_overhead = ac_cold - bl_cold
        if warm_saved is not None and warm_saved > 0:
            if cold_overhead is None or cold_overhead <= 0:
                break_even = 0
            else:
                break_even = math.ceil(cold_overhead / warm_saved)

    label, reason = suitability_verdict(
        files=files,
        warm_saving_ratio=warm_saving_ratio,
        break_even_passes=break_even,
    )

    return {
        "files": files,
        "measured": True,
        "cold_bytes": {
            "naive": naive_cold,
            "blobless": bl_cold,
            "agentcache": ac_cold,
        },
        "warm_bytes": {
            "naive": naive_warm,
            "blobless": bl_warm,
            "agentcache": ac_warm,
        },
        "saving_pct_vs_blobless": (
            round(warm_saving_ratio * 100, 1) if warm_saving_ratio is not None else None
        ),
        "break_even_passes": break_even,
        "verdict": label,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Async harness runner — starts daemon / proxy / service, runs experiment
# ---------------------------------------------------------------------------


async def _run_experiment(
    repos_dir: str,
    repo_name: str,
    git_port: int,
    proxy_port: int,
    svc_port: int,
    verbose: bool,
) -> dict:
    """Start daemon / proxy / service and run a two-pass experiment.

    Returns the first campaign's summary dict (with ``"error"`` key on failure).
    Always tears down the three processes in a ``finally`` block.
    """
    repo_dir = os.path.join(repos_dir, f"{repo_name}.git")

    # --- git daemon (serves the bare repo over git:// protocol) ---
    daemon = GitDaemon(repos_dir=repos_dir, port=git_port)
    daemon_ok = await daemon.start()
    if not daemon_ok:
        return {"error": f"git daemon failed to start on port {git_port}"}

    # --- byte-counting proxy (proxy_port → git_port, counts every byte) ---
    proxy = ByteCountingProxy("127.0.0.1", proxy_port, "127.0.0.1", git_port)
    await proxy.start()

    # --- agentcache service ---
    svc = AgentCacheService(port=svc_port)
    svc_ok = await svc.start(repo_dir)
    if not svc_ok and verbose:
        print(
            f"  [warn] agentcache service did not start on port {svc_port}; "
            "agentcache method will fall back to blobless"
        )

    try:
        runner = ExperimentRunner(
            proxy=proxy,
            agentcache_svc=svc,
            repos_dir=repos_dir,
            proxy_port=proxy_port,
            svc_port=svc_port,
        )

        exp_id = f"try-{uuid.uuid4().hex[:8]}"
        config = {
            # One repo — the one we mirrored.
            "repos": [f"{repo_name}.git"],
            # All three approaches to compare.
            "methods": ["naive", "blobless", "agentcache"],
            # passes=2: pass 1 is COLD (builds artifacts), pass 2 is WARM
            # (steady-state — the number the user actually cares about).
            "passes": 2,
            # Edit 2% of source files per pass — a representative light-touch
            # agent workload that runs fast on any repo size.
            "pct": 2.0,
            "seed": 1000,
            # No human commits in this quick trial.
            "human_commits": 0,
            # Build the cache right after cold pass so the warm pass has it.
            "hook_warms": True,
            # Host execution (no Docker needed).
            "use_docker": False,
        }

        log_lines: list[str] = []

        def emit(line: str) -> None:
            log_lines.append(line)
            if verbose:
                print(f"  {line}")

        result = await runner.run(exp_id, config, emit)
        campaigns = result.get("campaigns", [])
        if not campaigns:
            return {"error": "experiment produced no campaigns"}

        camp = campaigns[0]
        # Return the summary (with an error key if the campaign failed)
        if camp.get("error"):
            return {"error": camp["error"], "files": camp.get("files", 0)}

        summary = camp.get("summary", {})
        # Merge file count into summary so callers have it in one dict
        summary["_files"] = camp.get("files", 0)
        return summary

    finally:
        await svc.stop()
        await proxy.stop()
        await daemon.stop()


# ---------------------------------------------------------------------------
# Install-offer flow -- pure helpers + shell orchestrator
# ---------------------------------------------------------------------------


def should_offer_install(
    verdict_label: str,
    target_is_local_worktree: bool,
) -> bool:
    """Return True ONLY when AgentCache is genuinely worthwhile AND the target
    is a local, non-bare git worktree.

    Honest gating: we do NOT offer for "worth it only at high reuse",
    "blobless is enough", or "inconclusive" -- and never for URL targets or
    bare repos where we cannot scaffold files.

    Args:
        verdict_label:            The suitability label from suitability_verdict().
        target_is_local_worktree: True when the target is a local path that has a
                                  working tree (not bare, not a URL).

    Returns:
        True when the offer should be made, False otherwise.

    Examples:
        >>> should_offer_install("agentcache worthwhile", True)
        True
        >>> should_offer_install("agentcache worthwhile", False)
        False
        >>> should_offer_install("blobless is enough", True)
        False
    """
    return verdict_label == "agentcache worthwhile" and target_is_local_worktree


def detect_remote_kind(remote_url: Optional[str]) -> str:
    """Map a git remote URL to a hosting kind string.

    Args:
        remote_url: The URL from ``git remote get-url origin`` (or None/empty).

    Returns:
        ``"github"``  -- URL contains ``github.com`` (https or git@ssh).
        ``"other"``   -- Any other non-empty URL.
        ``"none"``    -- ``remote_url`` is None or empty.

    Examples:
        >>> detect_remote_kind("https://github.com/user/repo.git")
        'github'
        >>> detect_remote_kind("git@github.com:user/repo.git")
        'github'
        >>> detect_remote_kind("https://gitlab.com/user/repo.git")
        'other'
        >>> detect_remote_kind(None)
        'none'
        >>> detect_remote_kind("")
        'none'
    """
    if not remote_url:
        return "none"
    if "github.com" in remote_url:
        return "github"
    return "other"


def plan_scaffold(
    tooling_root: str,
    target_repo: str,
    remote_kind: str,
) -> list[dict]:
    """Return an ordered list of file-plan dicts describing what to scaffold.

    Each item has:
        dest   (str)  -- Relative path inside ``target_repo``.
        source (str)  -- Absolute path to the template file in ``tooling_root``.
        exists (bool) -- Whether ``dest`` already exists in ``target_repo``.
        action (str)  -- ``"create"`` or ``"skip-exists"`` (never overwrite).

    Items:
        1. ``.github/workflows/agentcache.yml`` -- ONLY when remote_kind == "github".
        2. ``AGENTS.md``  -- always.
        3. ``.agentcache`` -- always.

    Args:
        tooling_root: Root of the AgentCache tooling tree (has docs/ and
                      .agentcache.example).
        target_repo:  Root of the adopter's repo where files will be placed.
        remote_kind:  String from :func:`detect_remote_kind`.

    Returns:
        Ordered list of plan-item dicts (see above).
    """
    items: list[dict] = []

    def _item(dest: str, src_relpath: str) -> dict:
        source = os.path.join(tooling_root, src_relpath)
        full_dest = os.path.join(target_repo, dest)
        exists = os.path.exists(full_dest)
        action = "skip-exists" if exists else "create"
        return {"dest": dest, "source": source, "exists": exists, "action": action}

    if remote_kind == "github":
        items.append(
            _item(".github/workflows/agentcache.yml", "docs/adopter-workflow.yml")
        )

    items.append(_item("AGENTS.md", "docs/ADOPTER_AGENTS_TEMPLATE.md"))
    items.append(_item(".agentcache", ".agentcache.example"))

    return items


def apply_scaffold(plan: list[dict], target_repo: str) -> list[str]:
    """Execute a scaffold plan, writing ONLY ``action == "create"`` items.

    Parent directories are created automatically (``mkdir -p`` semantics).
    Existing files are never overwritten.  No git add/commit/push is ever
    performed.

    Args:
        plan:        List of plan-item dicts from :func:`plan_scaffold`.
        target_repo: Root of the adopter's repo.

    Returns:
        List of relative paths that were actually created (excludes skipped items).
    """
    created: list[str] = []
    for item in plan:
        if item["action"] != "create":
            continue
        dest_path = os.path.join(target_repo, item["dest"])
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(item["source"], dest_path)
        created.append(item["dest"])
    return created


def run_install_offer(
    *,
    verdict_label: str,
    target_is_local_worktree: bool,
    target_repo: str,
    tooling_root: str,
    remote_url: Optional[str],
    assume_yes: bool,
    no_install: bool,
    interactive: bool,
) -> dict:
    """Orchestrate the consent-gated scaffold offer.

    Decision tree:
      1. :func:`should_offer_install` -> False  ->  return {"offered": False}
      2. ``no_install``                       ->  print hint, return installed=False
      3. Build plan; prompt (or skip if assume_yes).
      4. On consent: :func:`apply_scaffold`, print summary.
      5. Return result dict.

    Prompt reads from ``/dev/tty`` (not stdin) so it works under ``curl | bash``
    where stdin is the piped script.  If ``/dev/tty`` cannot be opened and
    ``assume_yes`` is False and ``interactive`` is False, print a nudge and
    return without writing anything.

    NEVER calls git add/commit/push.

    Args:
        verdict_label:            Suitability label from :func:`suitability_verdict`.
        target_is_local_worktree: True when the target is a local non-bare repo.
        target_repo:              Root of the adopter's repo.
        tooling_root:             Root of the AgentCache tooling tree.
        remote_url:               URL from ``git remote get-url origin`` (or None).
        assume_yes:               Skip the y/N prompt and proceed directly.
        no_install:               Print hint only; do not scaffold anything.
        interactive:              True when stdout is a tty (``sys.stdout.isatty()``).

    Returns:
        Dict with at minimum ``{"offered": bool}``.  When ``installed`` is True
        the dict also includes ``"created"`` and ``"skipped"`` lists.
    """
    if not should_offer_install(verdict_label, target_is_local_worktree):
        return {"offered": False}

    remote_kind = detect_remote_kind(remote_url)

    if no_install:
        print(
            "\nAgentCache looks worthwhile -- re-run without --no-install "
            "(or see docs/INSTALL.md) to set it up."
        )
        return {"offered": True, "installed": False, "reason": "no_install"}

    plan = plan_scaffold(tooling_root, target_repo, remote_kind)

    # --- Gather consent ---
    if not assume_yes:
        files_to_create = [i["dest"] for i in plan if i["action"] == "create"]
        files_to_skip = [i["dest"] for i in plan if i["action"] == "skip-exists"]
        print("\nAgentCache looks worthwhile for this repo.")
        print("These files would be created in your repo:")
        for f in files_to_create:
            print(f"  + {f}")
        if files_to_skip:
            print("These already exist and will be left untouched:")
            for f in files_to_skip:
                print(f"  = {f} (already present, skipping)")
        if remote_kind in ("other", "none"):
            print(
                "\nNote: the GitHub Action template won't apply to your hosting "
                "platform.  AGENTS.md and .agentcache are server-agnostic and will "
                "still be created.  See docs/INSTALL.md for the self-hosted "
                "post-receive hook."
            )

        # Read consent from /dev/tty so this works under `curl | bash`.
        try:
            with open("/dev/tty") as tty:
                print("\nProceed? [y/N] ", end="", flush=True)
                response = tty.readline().strip().lower()
        except OSError:
            # /dev/tty is unavailable (non-interactive shell, pipe, etc.)
            if not interactive:
                print(
                    "\nAgentCache looks worthwhile.  Re-run interactively, "
                    "or pass --yes, to scaffold the setup files."
                )
                return {
                    "offered": True,
                    "installed": False,
                    "reason": "no_tty",
                }
            # Fallback: try stdin (user IS interactive)
            print("\nProceed? [y/N] ", end="", flush=True)
            response = sys.stdin.readline().strip().lower()

        if response not in ("y", "yes"):
            print("Skipping scaffold.")
            return {"offered": True, "installed": False, "reason": "declined"}

    # --- Apply scaffold ---
    created = apply_scaffold(plan, target_repo)
    skipped = [i["dest"] for i in plan if i["action"] == "skip-exists"]

    if created:
        print("\nCreated:")
        for f in created:
            print(f"  + {f}")
    if skipped:
        print("Already present (not overwritten):")
        for f in skipped:
            print(f"  = {f}")

    if remote_kind in ("other", "none"):
        print(
            "\nNote: your remote is not GitHub, so the GitHub Action template "
            "was not included.  AGENTS.md and .agentcache are server-agnostic.  "
            "See docs/INSTALL.md for the self-hosted post-receive hook."
        )

    print(
        "\nReview these files, then commit and push to activate.  "
        "The GitHub Action rebuilds the cache on every push to main.  "
        "Full guide: docs/INSTALL.md."
    )
    return {
        "offered": True,
        "installed": True,
        "created": created,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# CLI helpers for install-offer wiring
# ---------------------------------------------------------------------------


def _compute_target_is_local_worktree(target: str) -> bool:
    """Return True if *target* is a local, non-bare git worktree.

    Runs ``git -C <target> rev-parse --is-bare-repository`` (fast, read-only).
    Returns False for URL targets or bare repos.
    """
    if not _is_local_target(target):
        return False
    result = subprocess.run(
        ["git", "-C", target, "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "false"


def _read_origin_remote_url(target: str) -> Optional[str]:
    """Return ``origin`` remote URL for a local repo, or None on failure."""
    result = subprocess.run(
        ["git", "-C", target, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        return url if url else None
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.  Returns exit code (0 advisory; 1 on bad input)."""
    parser = argparse.ArgumentParser(
        prog="try_agentcache",
        description=(
            "Run this in the root of your repo (or pass a path / GitHub URL).\n\n"
            "Measure whether AgentCache would help YOUR repo, and by how much.\n\n"
            "Runs a real measurement through the testharness byte-counting proxy:\n"
            "  - COLD pass: builds / seeds agentcache artifacts\n"
            "  - WARM pass: steady-state per-agent network bytes\n\n"
            "Reports the saving vs blobless, break-even passes, and a verdict."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TARGET may be a local repo path or any git URL.\n"
            "If omitted, the current working directory is used (must be a git repo).\n"
            "Exit code is always 0 (advisory — never blocks anything).\n"
        ),
    )
    parser.add_argument(
        "target",
        metavar="TARGET",
        nargs="?",
        default=None,
        help=(
            "Local repo path or git URL (https://, git@…, file://…). "
            "Defaults to the current directory — run this from your repo root."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print harness log lines as the experiment runs",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        dest="yes",
        help=(
            "Automatically accept the offer to scaffold AgentCache files "
            "(skip the y/N prompt)"
        ),
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        dest="no_install",
        help="Print a hint about setup but do not scaffold any files",
    )
    args = parser.parse_args(argv)

    # Resolve TARGET: None / "." → cwd; explicit path/URL → use as-is.
    target = resolve_target(args.target, os.getcwd())

    # Validate local targets before starting any daemons.
    if _is_local_target(target):
        err = _validate_local_git_repo(target)
        if err:
            print(f"Error: {err}", file=sys.stderr)
            return 1

    # Collect local-repo metadata for the install-offer (fast git commands).
    target_is_local_worktree = _compute_target_is_local_worktree(target)
    remote_url: Optional[str] = (
        _read_origin_remote_url(target) if target_is_local_worktree else None
    )
    tooling_root = str(_ROOT)

    tmp_dir: Optional[str] = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="try_agentcache_")
        repos_dir = os.path.join(tmp_dir, "repos")
        os.makedirs(repos_dir)

        # --- Mirror the target repo ---
        if not args.json:
            print(f"\nMirroring {target!r} …", flush=True)
        try:
            name, _bare_path = _mirror_repo(target, repos_dir)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if not args.json:
            print(
                "Starting git daemon · byte-counting proxy · agentcache service …",
                flush=True,
            )

        # --- Pick free ephemeral ports to avoid colliding with any running harness ---
        git_port = _free_port()
        proxy_port = _free_port()
        svc_port = _free_port()

        if args.verbose:
            print(
                f"  Ephemeral ports: git={git_port}  proxy={proxy_port}  svc={svc_port}"
            )

        # --- Run the experiment ---
        t0 = time.monotonic()
        summary = asyncio.run(
            _run_experiment(
                repos_dir=repos_dir,
                repo_name=name,
                git_port=git_port,
                proxy_port=proxy_port,
                svc_port=svc_port,
                verbose=args.verbose,
            )
        )
        elapsed = time.monotonic() - t0

        files: int = summary.pop("_files", 0)

        # Compute the verdict label once for output + install-offer.
        json_data = try_result_json(summary, files)
        verdict_label: str = json_data.get("verdict", "")

        # --- Output ---
        if args.json:
            # Machine mode: add install_offered bool; never prompt or scaffold.
            json_data["install_offered"] = should_offer_install(
                verdict_label, target_is_local_worktree
            )
            print(json.dumps(json_data, indent=2))
        else:
            print()
            print(render_try_report(summary, files))
            print(f"\n  (Measurement took {elapsed:.1f}s)")

            # After printing the verdict, offer to scaffold if warranted.
            run_install_offer(
                verdict_label=verdict_label,
                target_is_local_worktree=target_is_local_worktree,
                target_repo=target,
                tooling_root=tooling_root,
                remote_url=remote_url,
                assume_yes=args.yes,
                no_install=args.no_install,
                interactive=sys.stdout.isatty(),
            )

        return 0

    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
