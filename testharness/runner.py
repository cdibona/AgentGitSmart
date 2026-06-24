"""Test runner: orchestrates all three approaches against the proxy.

Each approach is run in a thread pool (blocking subprocess) so we don't
stall the asyncio event loop.  Byte counts come from the proxy's
cumulative counters via snapshot→delta, so every byte that flows through
port 9419 during a run is attributed to that run.

Events pushed to the queue are plain dicts; the FastAPI SSE handler
serialises them to JSON for the browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import pygit2

log = logging.getLogger(__name__)

# Add repo root to path so benchmark.approaches.* is importable.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agentcache.config import AgentCacheConfig
from agentcache.hook import generate_for_commit
from benchmark.approaches import naive, blobless
from benchmark.approaches import agentcache as ac_approach
from .models import ApproachResult, PhaseBreakdown
from .proxy import ByteCountingProxy

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bench")


async def _in_thread(fn, *args) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, fn, *args)


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} TiB"


class TestRunner:
    def __init__(
        self,
        proxy: ByteCountingProxy,
        repos_dir: str,
        git_proxy_port: int,
        agentcache_port: int = 8765,
    ) -> None:
        self.proxy = proxy
        self.repos_dir = os.path.abspath(repos_dir)
        self.git_proxy_port = git_proxy_port
        self.agentcache_url = f"http://127.0.0.1:{agentcache_port}"

        # Active SSE queues keyed by run_id.
        self._queues: Dict[str, asyncio.Queue] = {}

    # ------------------------------------------------------------------
    # Queue management (for SSE streaming)
    # ------------------------------------------------------------------

    def register_queue(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[run_id] = q
        return q

    def _emit(self, run_id: str, event_type: str, **data) -> None:
        ev = {"type": event_type, "ts": datetime.now(timezone.utc).isoformat(), **data}
        q = self._queues.get(run_id)
        if q:
            q.put_nowait(ev)

    def _log(self, run_id: str, msg: str) -> None:
        log.info("[%s] %s", run_id, msg)
        self._emit(run_id, "log", msg=msg)

    # ------------------------------------------------------------------
    # Cache generation
    # ------------------------------------------------------------------

    def _ensure_cache(self, repo_path: str, commit_hex: str) -> None:
        """Generate agentcache artifacts if not already present."""
        repo = pygit2.Repository(repo_path)
        cfg = AgentCacheConfig(repo_dir=repo_path)
        ref_name = f"{cfg.ref_prefix}/{commit_hex}"
        if ref_name in repo.references:
            return  # already cached
        generate_for_commit(repo, commit_hex, cfg)

    # ------------------------------------------------------------------
    # Individual approach execution
    # ------------------------------------------------------------------

    def _run_naive(
        self,
        repo_url: str,
        branch: str,
        target_paths: List[str],
        work_dir: str,
    ) -> Dict[str, Any]:
        return naive.run(repo_url, branch, target_paths, work_dir)

    def _run_blobless(
        self,
        repo_url: str,
        commit: str,
        branch: str,
        target_paths: List[str],
        work_dir: str,
    ) -> Dict[str, Any]:
        return blobless.run(repo_url, commit, branch, target_paths, work_dir)

    def _run_agentcache(
        self,
        repo_url: str,
        commit: str,
        branch: str,
        target_paths: List[str],
        work_dir: str,
    ) -> Dict[str, Any]:
        return ac_approach.run(
            repo_url, commit, branch, self.agentcache_url, target_paths, work_dir
        )

    # ------------------------------------------------------------------
    # Main execution entry point
    # ------------------------------------------------------------------

    async def execute(
        self,
        run_id: str,
        repo_name: str,
        branch: str,
        target_paths: List[str],
        approaches: List[str],
        num_runs: int,
    ) -> List[ApproachResult]:
        """
        Run all requested approaches and return aggregated results.

        Events are pushed to the run's SSE queue throughout.
        """
        repo_path = os.path.join(self.repos_dir, repo_name)
        repo_url = f"git://127.0.0.1:{self.git_proxy_port}/{repo_name}"

        if not Path(repo_path).exists():
            self._emit(run_id, "error", msg=f"Repo not found: {repo_path}")
            raise FileNotFoundError(repo_path)

        # Resolve HEAD commit for the branch.
        repo = pygit2.Repository(repo_path)
        ref = repo.references.get(f"refs/heads/{branch}")
        if ref is None:
            raise ValueError(f"branch '{branch}' not found in {repo_name}")
        commit_hex = str(ref.peel(pygit2.Commit).id)
        self._log(run_id, f"Commit: {commit_hex[:12]} ({branch})")

        # Ensure git daemon is configured on this repo.
        try:
            repo.config["uploadpack.allowFilter"] = "true"
            repo.config["uploadpack.allowanysha1inwant"] = "true"
        except Exception:
            pass

        # Generate agentcache artifacts (fast no-op if already cached).
        if "agentcache" in approaches:
            self._log(run_id, "Generating agentcache cache (if needed)…")
            await _in_thread(self._ensure_cache, repo_path, commit_hex)
            self._log(run_id, "Cache ready.")

        # ---------------------------------------------------------------
        # Run each approach
        # ---------------------------------------------------------------
        results: List[ApproachResult] = []

        for approach in approaches:
            self._emit(run_id, "approach_start", approach=approach, total_runs=num_runs)
            run_metrics: List[Dict[str, Any]] = []

            for i in range(num_runs):
                self._log(run_id, f"[{approach}] run {i+1}/{num_runs}…")
                snap = self.proxy.snapshot()
                t0 = time.monotonic()

                try:
                    with tempfile.TemporaryDirectory(prefix="bench-") as wd:
                        if approach == "naive":
                            raw = await _in_thread(
                                self._run_naive, repo_url, branch, target_paths, wd
                            )
                        elif approach == "blobless":
                            raw = await _in_thread(
                                self._run_blobless, repo_url, commit_hex, branch, target_paths, wd
                            )
                        elif approach == "agentcache":
                            raw = await _in_thread(
                                self._run_agentcache, repo_url, commit_hex, branch, target_paths, wd
                            )
                        else:
                            raw = {"elapsed_s": 0.0}

                    elapsed = time.monotonic() - t0
                    delta = self.proxy.delta(snap)
                    raw["_proxy"] = delta
                    raw["elapsed_s"] = elapsed
                    run_metrics.append(raw)

                    bi = delta["bytes_in"]
                    bo = delta["bytes_out"]
                    self._log(
                        run_id,
                        f"  → {elapsed:.2f}s  "
                        f"↑{_fmt_bytes(bi)} ↓{_fmt_bytes(bo)} "
                        f"({_fmt_bytes(delta['bytes_total'])} total)",
                    )

                except Exception as exc:
                    self._log(run_id, f"  ERROR: {exc}")
                    run_metrics.append({"error": str(exc), "elapsed_s": 0.0})

            # Aggregate across runs
            result = self._aggregate(approach, run_metrics)
            self._emit(run_id, "approach_done", approach=approach, result=result.model_dump())
            results.append(result)

        self._emit(run_id, "run_complete", results=[r.model_dump() for r in results])
        q = self._queues.pop(run_id, None)
        if q:
            await q.put({"type": "stream_end"})
        return results

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(self, approach: str, runs: List[Dict[str, Any]]) -> ApproachResult:
        good = [r for r in runs if "error" not in r]
        if not good:
            return ApproachResult(
                approach=approach,
                elapsed_s=0.0,
                error=runs[0].get("error", "unknown") if runs else "no runs",
            )

        def avg(key: str, default=0) -> float:
            vals = [r.get(key, default) for r in good]
            return sum(vals) / len(vals)

        def avg_proxy(key: str) -> int:
            vals = [r.get("_proxy", {}).get(key, 0) for r in good]
            return int(sum(vals) / len(vals))

        # Phase breakdown (agentcache only)
        phases = None
        if approach == "agentcache" and any("phase_clone_s" in r for r in good):
            phases = PhaseBreakdown(
                clone_s=avg("phase_clone_s"),
                resolve_s=avg("phase_resolve_s"),
                fetch_s=avg("phase_fetch_s"),
            )

        return ApproachResult(
            approach=approach,
            elapsed_s=round(avg("elapsed_s"), 3),
            bytes_proxy_in=avg_proxy("bytes_in"),
            bytes_proxy_out=avg_proxy("bytes_out"),
            bytes_proxy_total=avg_proxy("bytes_total"),
            objects_received=int(avg("objects_received")),
            disk_bytes=int(avg("disk_bytes")),
            file_count=int(avg("file_count")),
            phases=phases,
        )
