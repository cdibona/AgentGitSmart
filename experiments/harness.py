"""Experiment harness: headless orchestration for the three agentcache studies.

Reuses the proven test-harness infrastructure (byte-counting proxy, git daemon,
agentcache service) and the real-agent task, but drives them in batch from
plain async code instead of the web UI.

A single ExperimentHarness owns:
  - one GitDaemon serving benchmark/repos/
  - one ByteCountingProxy in front of it (so we can measure per-run bytes)
  - one AgentCacheService that we point at whichever repo is under test

and exposes the primitives the experiments need:
  - clear_cache(repo)        -> wipe refs/agent-cache/* (start from nothing)
  - cache_ref_count(repo)    -> how many cache refs exist right now
  - run_agent(repo, method)  -> run one agent task, measured (bytes, timing, cache state)
  - install_hook / push_commit -> for the hook-update study
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pygit2

# Make the sibling testharness + agentcache packages importable.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from testharness.proxy import ByteCountingProxy  # noqa: E402
from testharness.processes import GitDaemon, AgentCacheService  # noqa: E402
from testharness.real_agent import run_real_agent  # noqa: E402
from agentcache import uninstall as uninstall_mod  # noqa: E402

REPOS_DIR = str(_ROOT / "benchmark" / "repos")
REF_PREFIX = "refs/agent-cache"

# The five collected, CPython-sized projects.
ALL_REPOS = ["cpython", "django", "go", "git", "redis"]


@dataclass
class RunResult:
    """One agent run, fully measured."""
    repo: str
    method: str               # naive | blobless | agentcache
    iteration: int
    seed: int
    # cache state observed around this run
    cache_existed_before: bool
    cache_refs_before: int
    cache_refs_after: int
    cache_built_this_run: bool   # cold build happened during this run
    # network + timing
    bytes_proxy_out: int
    bytes_proxy_in: int
    wall_s: float
    # agent-reported phase breakdown + outcome
    files_found: int = 0
    files_selected: int = 0
    files_modified: int = 0
    fetch_roundtrips: int = 0
    agentcache_detected: bool = False
    bundle_used: bool = False
    phase_clone_ms: float = 0.0
    phase_discover_ms: float = 0.0
    phase_fetch_ms: float = 0.0
    phase_commit_ms: float = 0.0
    commit_sha: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class ExperimentHarness:
    def __init__(
        self,
        *,
        git_port: int = 9520,
        proxy_port: int = 9521,
        svc_port: int = 8770,
        repos_dir: str = REPOS_DIR,
    ) -> None:
        self.git_port = git_port
        self.proxy_port = proxy_port
        self.svc_port = svc_port
        self.repos_dir = repos_dir
        self.daemon = GitDaemon(repos_dir, port=git_port)
        self.proxy = ByteCountingProxy("127.0.0.1", proxy_port, "127.0.0.1", git_port)
        self.service = AgentCacheService(port=svc_port)

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        ok = await self.daemon.start()
        if not ok:
            raise RuntimeError(f"git daemon failed to bind on {self.git_port}")
        await self.proxy.start()

    async def stop(self) -> None:
        await self.service.stop()
        await self.proxy.stop()
        await self.daemon.stop()

    # ── repo helpers ───────────────────────────────────────────────────
    def repo_dir(self, repo: str) -> str:
        return str(Path(self.repos_dir) / f"{repo}.git")

    def repo_url(self, repo: str) -> str:
        """URL through the proxy, so every byte is counted."""
        return f"git://127.0.0.1:{self.proxy_port}/{repo}.git"

    def head_commit(self, repo: str) -> str:
        out = subprocess.run(
            ["git", "--git-dir", self.repo_dir(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    def default_branch(self, repo: str) -> str:
        out = subprocess.run(
            ["git", "--git-dir", self.repo_dir(repo), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        return out.stdout.strip() or "main"

    def service_url(self) -> str:
        return f"http://127.0.0.1:{self.svc_port}"

    # ── cache state ────────────────────────────────────────────────────
    def cache_ref_count(self, repo: str) -> int:
        r = pygit2.Repository(self.repo_dir(repo))
        return len(uninstall_mod.find_cache_refs(r, REF_PREFIX))

    def cache_ref_for(self, repo: str, commit: str) -> bool:
        r = pygit2.Repository(self.repo_dir(repo))
        return f"{REF_PREFIX}/{commit}" in r.references

    def clear_cache(self, repo: str, *, gc: bool = False) -> int:
        """Erase all agentcache refs for *repo*. Returns count removed."""
        summary = uninstall_mod.erase(
            self.repo_dir(repo), ref_prefix=REF_PREFIX, gc=gc, dry_run=False
        )
        return summary["cache_ref_count"]

    async def use_repo(self, repo: str) -> bool:
        """Point the agentcache service at *repo* (restart if needed)."""
        return await self.service.switch_repo(self.repo_dir(repo))

    # ── the measured run ───────────────────────────────────────────────
    async def run_agent(
        self, repo: str, method: str, *, iteration: int, seed: int, pct: float = 2.0
    ) -> RunResult:
        commit = self.head_commit(repo)
        branch = self.default_branch(repo)

        # agentcache method needs the service pointed at this repo
        if method == "agentcache":
            await self.use_repo(repo)

        existed_before = self.cache_ref_for(repo, commit)
        refs_before = self.cache_ref_count(repo)

        snap = self.proxy.snapshot()
        t0 = time.monotonic()
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: run_real_agent(
                approach=method,
                repo_url=self.repo_url(repo),
                commit=commit,
                branch=branch,
                service_url=self.service_url(),
                pct=pct,
                seed=seed,
            ),
        )
        wall = time.monotonic() - t0
        delta = self.proxy.delta(snap)

        refs_after = self.cache_ref_count(repo)
        built = (not existed_before) and self.cache_ref_for(repo, commit)

        return RunResult(
            repo=repo,
            method=method,
            iteration=iteration,
            seed=seed,
            cache_existed_before=existed_before,
            cache_refs_before=refs_before,
            cache_refs_after=refs_after,
            cache_built_this_run=built,
            bytes_proxy_out=delta["bytes_out"],
            bytes_proxy_in=delta["bytes_in"],
            wall_s=round(wall, 3),
            files_found=result.get("files_found", 0),
            files_selected=result.get("files_selected", 0),
            files_modified=result.get("files_modified", 0),
            fetch_roundtrips=result.get("fetch_roundtrips", 0),
            agentcache_detected=result.get("agentcache_detected", False),
            bundle_used=result.get("bundle_used", False),
            phase_clone_ms=result.get("phase_clone_ms", 0.0),
            phase_discover_ms=result.get("phase_discover_ms", 0.0),
            phase_fetch_ms=result.get("phase_fetch_ms", 0.0),
            phase_commit_ms=result.get("phase_commit_ms", 0.0),
            commit_sha=result.get("commit_sha"),
            error=result.get("error"),
        )


# ── formatting helpers shared by the experiment scripts ────────────────

def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"
