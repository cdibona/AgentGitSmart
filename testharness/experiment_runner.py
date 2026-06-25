"""Comprehensive multi-project experiment runner for the web UI.

Where the single "run" (runner.py) compares approaches on ONE repo, this runs a
*campaign* across MANY repos and answers the questions the dashboard cares about:

  - 1st run (COLD, builds the cache) vs the average of later runs (WARM)
  - naive vs blobless vs agentcache, side by side
  - across multiple projects at once
  - what a HUMAN commit does to the cache (the per-commit cache is invalidated;
    with the server hook the next agent stays warm, without it goes cold again)

It reuses the harness infrastructure that is already running inside the web app
(the byte-counting proxy, the git daemon, the agentcache service) so nothing new
binds a port, and it reuses the real-agent task so the measurements match.

Design notes
------------
* Agent passes are read-only: they clone into a throwaway temp dir, so running
  many of them never mutates the shared bare repo.
* A HUMAN pass DOES need to move HEAD (that is the whole point — it invalidates
  the commit-keyed cache).  To stay non-destructive we do that on a throwaway
  branch ``agentcache-exp/<id>`` created from HEAD; agents then target that
  branch, and everything (branch + generated cache refs) is deleted at the end.
* The human commit is created server-side with pure plumbing (no push, no daemon
  reconfig).  If ``hook_warms`` is set we then pre-build the cache for the new
  commit — exactly what a real ``post-receive`` hook would do.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import pygit2

from .real_agent import run_real_agent
from agentcache import uninstall as uninstall_mod
from agentcache.config import AgentCacheConfig
from agentcache import hook as hook_mod

REF_PREFIX = "refs/agent-cache"
EXP_BRANCH_PREFIX = "agentcache-exp"


class ExperimentRunner:
    def __init__(
        self,
        *,
        proxy,
        agentcache_svc,
        repos_dir: str,
        proxy_port: int,
        svc_port: int,
    ) -> None:
        self.proxy = proxy
        self.svc = agentcache_svc
        self.repos_dir = repos_dir
        self.proxy_port = proxy_port
        self.svc_port = svc_port

    # ── small repo helpers ─────────────────────────────────────────────
    def repo_dir(self, repo: str) -> str:
        return str(Path(self.repos_dir) / repo)

    def repo_url(self, repo: str) -> str:
        return f"git://127.0.0.1:{self.proxy_port}/{repo}"

    def service_url(self) -> str:
        return f"http://127.0.0.1:{self.svc_port}"

    def _git(self, repo_dir: str, *args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "--git-dir", repo_dir, *args],
                              capture_output=True, text=True, **kw)

    def head_commit(self, repo_dir: str, ref: str) -> str:
        return self._git(repo_dir, "rev-parse", ref).stdout.strip()

    def default_branch(self, repo_dir: str) -> str:
        out = self._git(repo_dir, "symbolic-ref", "--short", "HEAD").stdout.strip()
        return out or "main"

    def file_count(self, repo_dir: str, ref: str) -> int:
        r = self._git(repo_dir, "ls-tree", "-r", "--name-only", ref)
        return len(r.stdout.splitlines()) if r.returncode == 0 else 0

    def cache_ref_exists(self, repo_dir: str, commit: str) -> bool:
        return f"{REF_PREFIX}/{commit}" in pygit2.Repository(repo_dir).references

    def cache_ref_count(self, repo_dir: str) -> int:
        return len(uninstall_mod.find_cache_refs(pygit2.Repository(repo_dir), REF_PREFIX))

    def clear_cache(self, repo_dir: str) -> int:
        return uninstall_mod.erase(repo_dir, ref_prefix=REF_PREFIX, dry_run=False)[
            "cache_ref_count"
        ]

    # ── the measured agent pass ────────────────────────────────────────
    async def _agent_pass(
        self, repo: str, repo_dir: str, branch: str, commit: str,
        method: str, seed: int, pct: float,
    ) -> dict:
        if method == "agentcache":
            await self.svc.switch_repo(repo_dir)

        cold = (method == "agentcache") and not self.cache_ref_exists(repo_dir, commit)
        # Honest cold: on a genuinely cold commit (no cache ref yet) there is no
        # CDN bundle either, so agentcache must pull full history through the
        # network — the real first-visit cost.  Only warm passes get the bundle.
        use_bundle = not cold
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
                use_bundle=use_bundle,
            ),
        )
        wall = time.monotonic() - t0
        delta = self.proxy.delta(snap)
        return {
            "bytes": delta["bytes_out"],
            "bytes_sent": delta["bytes_in"],
            "wall_s": round(wall, 3),
            "roundtrips": result.get("fetch_roundtrips", 0),
            "cold": bool(cold),
            "bundle_used": result.get("bundle_used", False),
            "agentcache_detected": result.get("agentcache_detected", False),
            "files_found": result.get("files_found", 0),
            "files_selected": result.get("files_selected", 0),
            "modified": result.get("files_modified", 0),
            "phase_clone_ms": round(result.get("phase_clone_ms", 0.0), 1),
            "phase_discover_ms": round(result.get("phase_discover_ms", 0.0), 1),
            "phase_fetch_ms": round(result.get("phase_fetch_ms", 0.0), 1),
            "phase_commit_ms": round(result.get("phase_commit_ms", 0.0), 1),
            "error": result.get("error"),
        }

    # ── the human pass (moves HEAD on a throwaway branch) ──────────────
    def _human_commit(self, repo_dir: str, branch: str, n: int) -> str:
        """Create a new commit on refs/heads/<branch> via plumbing. Returns new oid."""
        ref = f"refs/heads/{branch}"
        parent = self.head_commit(repo_dir, ref)
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = os.path.join(repo_dir, f".exp-index-{n}")
        if os.path.exists(env["GIT_INDEX_FILE"]):
            os.remove(env["GIT_INDEX_FILE"])
        # Seed the index from the parent tree, then add one tiny new file so the
        # tree (and thus the commit OID) genuinely changes.
        subprocess.run(["git", "--git-dir", repo_dir, "read-tree", parent],
                       capture_output=True, text=True, env=env)
        blob = subprocess.run(
            ["git", "--git-dir", repo_dir, "hash-object", "-w", "--stdin"],
            input=f"# teammate change #{n}\n", capture_output=True, text=True, env=env,
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", repo_dir, "update-index", "--add",
             "--cacheinfo", f"100644,{blob},TEAMMATE_NOTES_{n}.md"],
            capture_output=True, text=True, env=env,
        )
        tree = subprocess.run(["git", "--git-dir", repo_dir, "write-tree"],
                              capture_output=True, text=True, env=env).stdout.strip()
        commit = subprocess.run(
            ["git", "--git-dir", repo_dir, "commit-tree", tree, "-p", parent,
             "-m", f"teammate: push #{n}"],
            capture_output=True, text=True,
            env={**env, "GIT_AUTHOR_NAME": "A Teammate", "GIT_AUTHOR_EMAIL": "team@human",
                 "GIT_COMMITTER_NAME": "A Teammate", "GIT_COMMITTER_EMAIL": "team@human"},
        ).stdout.strip()
        subprocess.run(["git", "--git-dir", repo_dir, "update-ref", ref, commit],
                       capture_output=True, text=True)
        try:
            os.remove(env["GIT_INDEX_FILE"])
        except OSError:
            pass
        return commit

    def _hook_build(self, repo_dir: str, commit: str) -> None:
        """Pre-build the cache for *commit*, exactly as the post-receive hook would."""
        cfg = AgentCacheConfig(repo_dir=repo_dir)
        repo = pygit2.Repository(repo_dir)
        hook_mod.generate_for_commit(repo, commit, cfg)

    # ── one repo's campaign ────────────────────────────────────────────
    async def _campaign(
        self, repo: str, config: dict, emit: Callable[[str], None]
    ) -> dict:
        repo_dir = self.repo_dir(repo)
        methods = config["methods"]
        passes = config["passes"]
        pct = config["pct"]
        seed0 = config["seed"]
        human_commits = int(config.get("human_commits", 0) or 0)
        hook_warms = config["hook_warms"]
        exp_id = config["_exp_id"]

        # Start every campaign from a clean (cold) cache.
        cleared = self.clear_cache(repo_dir)
        created_branch: Optional[str] = None
        if human_commits > 0:
            # Work on a throwaway branch so HEAD moves are non-destructive.
            base = self.default_branch(repo_dir)
            base_commit = self.head_commit(repo_dir, f"refs/heads/{base}")
            branch = f"{EXP_BRANCH_PREFIX}/{exp_id}-{repo}"
            self._git(repo_dir, "update-ref", f"refs/heads/{branch}", base_commit)
            created_branch = branch
        else:
            branch = self.default_branch(repo_dir)

        remaining = self.cache_ref_count(repo_dir)
        commit = self.head_commit(repo_dir, f"refs/heads/{branch}")
        nfiles = self.file_count(repo_dir, commit)
        emit(f"[{repo}] start — {nfiles} files · cleared {cleared} agent-cache "
             f"ref(s), {remaining} remain (verified cold)")

        # Spread the requested human commits across the gaps between agent
        # passes, one per gap, starting right after pass 1.  With G = passes-1
        # gaps we can place at most G commits; anything beyond that is ignored
        # (there is no later agent pass for it to affect).
        gaps = max(0, passes - 1)
        commits_to_place = min(human_commits, gaps) if gaps else 0
        # gap after pass i (1-based) gets a human commit for i in 1..commits_to_place
        timeline: list[dict] = []
        human_count = 0

        for i in range(1, passes + 1):
            cells = {}
            for method in methods:
                cell = await self._agent_pass(
                    repo, repo_dir, branch, commit, method, seed0 + i, pct
                )
                cells[method] = cell
                tag = "COLD" if cell["cold"] else ""
                emit(
                    f"[{repo}] pass {i} {method:10} {cell['bytes']:>10,}B "
                    f"{cell['wall_s']:.2f}s {tag}"
                    + (f" ERR={cell['error'][:40]}" if cell["error"] else "")
                )
            timeline.append({"pass_index": i, "kind": "agent", "commit": commit, "cells": cells})

            # Insert a human commit in this gap (after pass i), if budgeted.
            if i <= commits_to_place:
                human_count += 1
                new_commit = self._human_commit(repo_dir, branch, human_count)
                warmed = False
                if hook_warms:
                    self._hook_build(repo_dir, new_commit)
                    warmed = True
                timeline.append({
                    "pass_index": i, "kind": "human", "commit": new_commit,
                    "human_index": human_count, "hook_warmed": warmed,
                    "note": f"teammate commit #{human_count}"
                            + (" (hook pre-warmed cache)" if warmed else ""),
                })
                commit = new_commit  # subsequent agent passes target the new HEAD
                emit(f"[{repo}] HUMAN commit #{human_count} {new_commit[:10]} "
                     + ("(hook pre-warmed)" if warmed else "(no hook → next agent cold)"))

        summary = _summarize_campaign(timeline, methods)

        # Cleanup: remove the throwaway branch and any cache refs we created.
        if created_branch:
            self._git(repo_dir, "update-ref", "-d", f"refs/heads/{created_branch}")
        self.clear_cache(repo_dir)

        return {"repo": repo, "files": nfiles, "timeline": timeline, "summary": summary}

    # ── public entry ───────────────────────────────────────────────────
    async def run(self, exp_id: str, config: dict, emit: Callable[[str], None]) -> dict:
        config = dict(config)
        config["_exp_id"] = exp_id
        campaigns = []
        for repo in config["repos"]:
            try:
                campaigns.append(await self._campaign(repo, config, emit))
            except Exception as exc:  # one repo failing must not kill the experiment
                emit(f"[{repo}] FAILED: {exc}")
                campaigns.append({"repo": repo, "files": 0, "timeline": [],
                                  "summary": {}, "error": str(exc)})
        return {"campaigns": campaigns}


def _summarize_campaign(timeline: list[dict], methods: list[str]) -> dict:
    """Per-method cold (1st) vs warm (avg of rest) bytes + wall, and win vs naive."""
    agent_passes = [p for p in timeline if p["kind"] == "agent"]
    out: dict = {}
    for m in methods:
        cells = [p["cells"][m] for p in agent_passes if m in p["cells"] and not p["cells"][m]["error"]]
        if not cells:
            out[m] = {"cold_bytes": None, "warm_avg_bytes": None,
                      "cold_wall": None, "warm_avg_wall": None, "runs": 0}
            continue
        cold = cells[0]
        warm = cells[1:] or cells  # if only one pass, warm == that pass
        out[m] = {
            "cold_bytes": cold["bytes"],
            "warm_avg_bytes": round(sum(c["bytes"] for c in warm) / len(warm)),
            "cold_wall": cold["wall_s"],
            "warm_avg_wall": round(sum(c["wall_s"] for c in warm) / len(warm), 3),
            "cold_roundtrips": cold.get("roundtrips", 0),
            "warm_avg_roundtrips": round(sum(c.get("roundtrips", 0) for c in warm) / len(warm), 1),
            "cold_bundle_used": cold.get("bundle_used", False),
            "warm_bundle_used": warm[0].get("bundle_used", False),
            "cold_clone_ms": cold.get("phase_clone_ms", 0.0),
            "warm_avg_clone_ms": round(sum(c.get("phase_clone_ms", 0.0) for c in warm) / len(warm), 1),
            "runs": len(cells),
        }
    # win factors vs naive (warm averages) — both bytes and wall time.
    naive_warm = out.get("naive", {}).get("warm_avg_bytes")
    naive_warm_wall = out.get("naive", {}).get("warm_avg_wall")
    wins, wins_wall = {}, {}
    for m in methods:
        mw = out.get(m, {}).get("warm_avg_bytes")
        wins[m] = round(naive_warm / mw, 1) if (naive_warm and mw) else None
        mwall = out.get(m, {}).get("warm_avg_wall")
        wins_wall[m] = round(naive_warm_wall / mwall, 1) if (naive_warm_wall and mwall) else None
    out["_win_vs_naive"] = wins
    out["_win_vs_naive_wall"] = wins_wall
    return out
