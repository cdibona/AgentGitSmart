"""Comprehensive multi-project experiment runner for the web UI.

Where the single "run" (runner.py) compares approaches on ONE repo, this runs a
*campaign* across MANY repos and answers the questions the dashboard cares about:

  - 1st run (COLD, builds the cache) vs the average of later runs (WARM)
  - naive vs blobless vs agentgitsmart, side by side
  - across multiple projects at once
  - what a HUMAN commit does to the cache (the per-commit cache is invalidated;
    with the server hook the next agent stays warm, without it goes cold again)

It reuses the harness infrastructure that is already running inside the web app
(the byte-counting proxy, the git daemon, the agentgitsmart service) so nothing new
binds a port, and it reuses the real-agent task so the measurements match.

Design notes
------------
* Agent passes are read-only: they clone into a throwaway temp dir, so running
  many of them never mutates the shared bare repo.
* A HUMAN pass DOES need to move HEAD (that is the whole point — it invalidates
  the commit-keyed cache).  To stay non-destructive we do that on a throwaway
  branch ``agentgitsmart-exp/<id>`` created from HEAD; agents then target that
  branch, and everything (branch + generated cache refs) is deleted at the end.
* The human commit is created server-side with pure plumbing (no push, no daemon
  reconfig).  If ``hook_warms`` is set we then pre-build the cache for the new
  commit — exactly what a real ``post-receive`` hook would do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pygit2

from . import docker_runner
from .real_agent import run_real_agent
from agentgitsmart import bundle as bundle_mod
from agentgitsmart import cache_writer
from agentgitsmart import hook as hook_mod
from agentgitsmart import uninstall as uninstall_mod
from agentgitsmart.config import AgentGitSmartConfig

REF_PREFIX = "refs/agent-git-smart"
EXP_BRANCH_PREFIX = "agentgitsmart-exp"
# Project root is the parent of testharness/; scripts/ lives there.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
# Local bundle directory that _find_bundle() searches on non-Docker runs.
_LOCAL_BUNDLE_DIR = Path(__file__).resolve().parent.parent / "benchmark" / "bundles"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level pure helpers — testable without instantiating ExperimentRunner
# ---------------------------------------------------------------------------


def should_use_bundle(method: str, is_cold: bool, cold_bundle: bool) -> bool:
    """Return whether this agent pass should present a pre-built bundle.

    Warm passes (is_cold=False) always use the bundle — that is the steady-state
    path.  Cold passes follow the config:

    * cold_bundle=False (default): honest first-visit — no bundle, so the full
      history cost flows through the git daemon exactly as a real first agent on
      a fresh commit would pay it.
    * cold_bundle=True: production-amortised cold — the bundle was pre-built once
      per commit (as the CDN would serve it), so this pass pays only the tiny
      delta and blob fetch, not the full history.

    The cold_bundle flag is only meaningful for the agentgitsmart method; naive and
    blobless do not use bundles at all, so cold naive/blobless always return
    False regardless.
    """
    return (not is_cold) or (method == "agentgitsmart" and cold_bundle)


# ---------------------------------------------------------------------------
# Module-level helper — testable without instantiating ExperimentRunner
# ---------------------------------------------------------------------------


def run_action_generate(
    repo_dir: str,
    commit: str,
    branch: str,
    scripts_dir: Path = SCRIPTS_DIR,
) -> dict:
    """Run scripts/generate_agentgitsmart.py as a subprocess (GitHub Action equivalent).

    Builds manifest + symbols + cache ref AND a blobless bundle (the extra work
    the in-process hook does NOT do).  Times the full subprocess wall clock,
    reads the generation block back from the written meta.json artifact, and
    captures the bundle file size.

    Returns a dict:
        wall_s       -- elapsed seconds (float, 4dp)
        generation   -- the "generation" block from meta.json ({} on failure)
        bundle_bytes -- total bytes of *.bundle files written (0 on failure)
        returncode   -- subprocess exit code (-1 on internal exception)
        error        -- last 500 chars of stderr when returncode != 0, else None

    Never raises.
    """
    bundle_dir = tempfile.mkdtemp(prefix="agentgitsmart_action_")
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(
            [
                sys.executable,
                str(scripts_dir / "generate_agentgitsmart.py"),
                "--repo",
                repo_dir,
                "--commit",
                commit,
                "--branch",
                branch,
                "--bundle-dir",
                bundle_dir,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        elapsed = time.perf_counter() - t0

        gen: dict = {}
        if proc.returncode == 0:
            try:
                repo = pygit2.Repository(repo_dir)
                raw = cache_writer.read_artifact(repo, commit, "meta.json")
                gen = json.loads(raw).get("generation", {})
            except Exception:
                pass  # generation block unavailable — return empty dict

        bundle_bytes: int = sum(
            p.stat().st_size for p in Path(bundle_dir).glob("*.bundle")
        )
        return {
            "wall_s": round(elapsed, 4),
            "generation": gen,
            "bundle_bytes": bundle_bytes,
            "returncode": proc.returncode,
            "error": proc.stderr[-500:] if proc.returncode != 0 else None,
        }
    except Exception as exc:
        return {
            "wall_s": 0.0,
            "generation": {},
            "bundle_bytes": 0,
            "returncode": -1,
            "error": str(exc),
        }
    finally:
        shutil.rmtree(bundle_dir, ignore_errors=True)


class ExperimentRunner:
    def __init__(
        self,
        *,
        proxy,
        agentgitsmart_svc,
        repos_dir: str,
        proxy_port: int,
        svc_port: int,
    ) -> None:
        self.proxy = proxy
        self.svc = agentgitsmart_svc
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
        return subprocess.run(
            ["git", "--git-dir", repo_dir, *args], capture_output=True, text=True, **kw
        )

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
        return len(
            uninstall_mod.find_cache_refs(pygit2.Repository(repo_dir), REF_PREFIX)
        )

    def clear_cache(self, repo_dir: str) -> int:
        return uninstall_mod.erase(repo_dir, ref_prefix=REF_PREFIX, dry_run=False)[
            "cache_ref_count"
        ]

    # ── the measured agent pass ────────────────────────────────────────
    async def _agent_pass(
        self,
        repo: str,
        repo_dir: str,
        branch: str,
        commit: str,
        method: str,
        seed: int,
        pct: float,
        use_docker: bool,
        cold_bundle: bool = False,
    ) -> dict:
        if method == "agentgitsmart":
            await self.svc.switch_repo(repo_dir)

        cold = (method == "agentgitsmart") and not self.cache_ref_exists(repo_dir, commit)
        use_bundle = should_use_bundle(method, is_cold=cold, cold_bundle=cold_bundle)
        loop = asyncio.get_event_loop()

        snap = self.proxy.snapshot()
        t0 = time.monotonic()
        cpu_pct = 0.0
        used_docker = False
        if use_docker:
            # Fresh, disposable container per pass — true process/fs isolation.
            # --network=host keeps the clone/fetch flowing through the counting
            # proxy, so bytes are still measured; CPU is sampled via cgroups.
            payload = await loop.run_in_executor(
                None,
                lambda: docker_runner.run_in_container(
                    approach=method,
                    repo_url=self.repo_url(repo),
                    commit=commit,
                    branch=branch,
                    target_paths=[],
                    service_url=self.service_url(),
                    symbol="",
                    use_real_agent=True,
                    agent_pct=pct,
                    agent_seed=seed,
                    use_bundle=use_bundle,
                ),
            )
            result = payload.get("real_agent_data", payload) or {}
            samples = payload.get("cpu_samples", []) or []
            # CpuSampler.samples are dicts {"t_ms", "cpu_pct"} (see metrics.py),
            # NOT (t, pct) tuples.  Indexing with s[1] raised KeyError(1) whose
            # str() is the opaque "1" — and only on passes long enough for the
            # 0.2s cgroup sampler to capture a sample (e.g. cold agentgitsmart on a
            # larger repo), which is why fast passes silently "worked".
            cpu_pct = round(max((s["cpu_pct"] for s in samples), default=0.0), 1)
            used_docker = True
        else:
            result = await loop.run_in_executor(
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
            "agentgitsmart_detected": result.get("agentgitsmart_detected", False),
            "files_found": result.get("files_found", 0),
            "files_selected": result.get("files_selected", 0),
            "modified": result.get("files_modified", 0),
            "cpu_pct": cpu_pct,
            "used_docker": used_docker,
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
        subprocess.run(
            ["git", "--git-dir", repo_dir, "read-tree", parent],
            capture_output=True,
            text=True,
            env=env,
        )
        blob = subprocess.run(
            ["git", "--git-dir", repo_dir, "hash-object", "-w", "--stdin"],
            input=f"# teammate change #{n}\n",
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "--git-dir",
                repo_dir,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},TEAMMATE_NOTES_{n}.md",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        tree = subprocess.run(
            ["git", "--git-dir", repo_dir, "write-tree"],
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()
        commit = subprocess.run(
            [
                "git",
                "--git-dir",
                repo_dir,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                f"teammate: push #{n}",
            ],
            capture_output=True,
            text=True,
            env={
                **env,
                "GIT_AUTHOR_NAME": "A Teammate",
                "GIT_AUTHOR_EMAIL": "team@human",
                "GIT_COMMITTER_NAME": "A Teammate",
                "GIT_COMMITTER_EMAIL": "team@human",
            },
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", repo_dir, "update-ref", ref, commit],
            capture_output=True,
            text=True,
        )
        try:
            os.remove(env["GIT_INDEX_FILE"])
        except OSError:
            pass
        return commit

    def _hook_build(self, repo_dir: str, commit: str) -> dict:
        """Pre-build the cache for *commit*, exactly as the post-receive hook would.

        Returns the ``generation`` (load) block so the human step that triggered
        it can be measured: mode (full/delta), files reindexed/carried, bytes
        materialized, symbol count, fallback reason, etc.
        """
        cfg = AgentGitSmartConfig(repo_dir=repo_dir)
        repo = pygit2.Repository(repo_dir)
        result = hook_mod.generate_for_commit(repo, commit, cfg)
        return result.get("generation", {}) if isinstance(result, dict) else {}

    def _delete_cache_ref(self, repo_dir: str, commit: str) -> None:
        """Delete refs/agent-git-smart/<commit> if it exists; no-op otherwise."""
        repo = pygit2.Repository(repo_dir)
        ref_name = f"{REF_PREFIX}/{commit}"
        if ref_name in repo.references:
            repo.references[ref_name].delete()

    def _action_build(self, repo_dir: str, commit: str, branch: str) -> dict:
        """Run the GitHub Action equivalent as a subprocess.

        Delegates to the module-level :func:`run_action_generate` so the core
        logic can be unit-tested without instantiating ExperimentRunner.

        Returns the dict from :func:`run_action_generate` — wall_s, generation,
        bundle_bytes, returncode, error.  Never raises.
        """
        return run_action_generate(repo_dir, commit, branch)

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
        use_docker = bool(config.get("_use_docker", False))
        cold_bundle = bool(config.get("cold_bundle", False))
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
        emit(
            f"[{repo}] start — {nfiles} files · cleared {cleared} agent-git-smart "
            f"ref(s), {remaining} remain (verified cold)"
        )

        # When cold_bundle is set, pre-build a blobless bundle so cold agentgitsmart
        # passes can use it (production-amortised cold path).  We write it to the
        # same directory and with the same filename that _find_bundle() expects, so
        # it is picked up automatically.  Failure is guarded: a build error never
        # crashes the campaign — the pass simply falls back to bundle_used=False.
        if cold_bundle and "agentgitsmart" in methods:
            _bundle_path = _LOCAL_BUNDLE_DIR / f"{repo}-{branch}.bundle"
            try:
                _repo_obj = pygit2.Repository(repo_dir)
                bundle_mod.create_blobless_bundle(
                    _repo_obj,
                    f"refs/heads/{branch}",
                    str(_bundle_path),
                )
                emit(
                    f"[{repo}] cold_bundle: pre-built blobless bundle "
                    f"({_bundle_path.stat().st_size:,} B) → {_bundle_path.name}"
                )
            except Exception as _exc:
                emit(
                    f"[{repo}] cold_bundle: bundle build FAILED "
                    f"(cold passes will be bundle-free — bundle_used=False): {_exc}"
                )

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
            pass_started_at = datetime.now(timezone.utc).isoformat()
            for method in methods:
                cell = await self._agent_pass(
                    repo,
                    repo_dir,
                    branch,
                    commit,
                    method,
                    seed0 + i,
                    pct,
                    use_docker,
                    cold_bundle=cold_bundle,
                )
                cells[method] = cell
                tag = "COLD" if cell["cold"] else ""
                emit(
                    f"[{repo}] pass {i} {method:10} {cell['bytes']:>10,}B "
                    f"{cell['wall_s']:.2f}s {tag}"
                    + (f" ERR={cell['error'][:40]}" if cell["error"] else "")
                )
            timeline.append(
                {
                    "pass_index": i,
                    "kind": "agent",
                    "commit": commit,
                    "started_at": pass_started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "cells": cells,
                }
            )

            # Insert a human commit in this gap (after pass i), if budgeted.
            if i <= commits_to_place:
                human_count += 1
                warm_method = config.get("warm_method", "hook")
                human_started_at = datetime.now(timezone.utc).isoformat()
                _t0 = time.perf_counter()
                new_commit = self._human_commit(repo_dir, branch, human_count)
                commit_wall_s = time.perf_counter() - _t0

                warmed = False
                hook_gen: Optional[dict] = None
                hook_wall_s = 0.0
                action_dict: Optional[dict] = None
                comparison: Optional[dict] = None

                if hook_warms:
                    if warm_method == "hook":
                        _th = time.perf_counter()
                        hook_gen = self._hook_build(repo_dir, new_commit)
                        hook_wall_s = time.perf_counter() - _th
                        warmed = True
                    elif warm_method == "action":
                        action_dict = self._action_build(repo_dir, new_commit, branch)
                        warmed = True
                    elif warm_method == "both":
                        # Run each from a cold start to measure fairly.
                        # Defensive clear (no-op right after a fresh human commit, but
                        # ensures cold start if re-running against an existing commit).
                        self._delete_cache_ref(repo_dir, new_commit)
                        _th = time.perf_counter()
                        hook_gen = self._hook_build(repo_dir, new_commit)
                        hook_wall_s = time.perf_counter() - _th
                        # Clear the ref so the action also starts cold.
                        self._delete_cache_ref(repo_dir, new_commit)
                        action_dict = self._action_build(repo_dir, new_commit, branch)
                        # Leave the action-built cache in place (it also has the bundle).
                        h = hook_wall_s
                        a = action_dict.get("wall_s", 0.0)
                        comparison = {
                            "hook_wall_s": round(h, 4),
                            "action_wall_s": round(a, 4),
                            "ratio_action_over_hook": round(a / h, 2) if h else None,
                            "faster": "hook" if h <= a else "action",
                        }
                        warmed = True

                # Build the note suffix based on which mechanism(s) ran.
                if warmed:
                    _note_suffix = {
                        "hook": " (hook pre-warmed cache)",
                        "action": " (action pre-warmed cache)",
                        "both": " (hook+action pre-warmed cache — compared)",
                    }.get(warm_method, " (pre-warmed cache)")
                else:
                    _note_suffix = ""

                entry: dict = {
                    "pass_index": i,
                    "kind": "human",
                    "commit": new_commit,
                    "human_index": human_count,
                    "hook_warmed": warmed,
                    "warm_method": warm_method,
                    "started_at": human_started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "files_changed": 1,  # the teammate adds exactly one file
                    "commit_wall_s": round(commit_wall_s, 4),
                    # hook_wall_s / hook kept for backward compat; 0.0/None when not used.
                    "hook_wall_s": round(hook_wall_s, 4),
                    "hook": hook_gen,
                    "note": f"teammate commit #{human_count}{_note_suffix}",
                }
                if warm_method in ("action", "both"):
                    entry["action"] = action_dict
                if warm_method == "both":
                    entry["comparison"] = comparison
                timeline.append(entry)

                commit = new_commit  # subsequent agent passes target the new HEAD

                # Emit a human-readable summary of what the warm step did.
                if hook_warms:
                    if warm_method == "hook":
                        if hook_gen:
                            emit(
                                f"[{repo}] HUMAN commit #{human_count} {new_commit[:10]} "
                                f"→ hook {hook_gen.get('mode', '?')}: "
                                f"reindexed={hook_gen.get('files_reindexed', '?')} "
                                f"carried={hook_gen.get('files_carried_forward', '?')} "
                                f"{hook_gen.get('content_bytes_materialized', '?'):,}B "
                                f"{hook_wall_s:.2f}s"
                            )
                        else:
                            emit(
                                f"[{repo}] HUMAN commit #{human_count} {new_commit[:10]} "
                                "(hook ran, no generation data)"
                            )
                    elif warm_method == "action":
                        a = action_dict.get("wall_s", 0.0) if action_dict else 0.0
                        bundle_bytes = (
                            action_dict.get("bundle_bytes", 0) if action_dict else 0
                        )
                        emit(
                            f"[{repo}] HUMAN #{human_count} {new_commit[:10]} "
                            f"→ action {a:.2f}s (bundle {bundle_bytes:,}B)"
                        )
                    elif warm_method == "both":
                        h = hook_wall_s
                        a = action_dict.get("wall_s", 0.0) if action_dict else 0.0
                        bundle_bytes = (
                            action_dict.get("bundle_bytes", 0) if action_dict else 0
                        )
                        emit(
                            f"[{repo}] HUMAN #{human_count} {new_commit[:10]} "
                            f"warm compare → hook {h:.2f}s vs action {a:.2f}s "
                            f"(bundle {bundle_bytes:,}B)"
                        )
                else:
                    emit(
                        f"[{repo}] HUMAN commit #{human_count} {new_commit[:10]} "
                        "(no hook → next agent cold)"
                    )

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

        # Resolve container isolation: requested AND docker available AND image
        # builds.  Otherwise degrade gracefully to host execution (still fully
        # measured through the proxy) and say so in the log.
        want_docker = bool(config.get("use_docker", True))
        use_docker = False
        if want_docker:
            if docker_runner.is_docker_available():
                emit("Docker available — building/checking agent image…")
                ok = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: docker_runner.ensure_image(emit)
                )
                use_docker = bool(ok)
                emit(
                    "Each pass will run in a FRESH disposable container 🐳"
                    if use_docker
                    else "Docker image unavailable — running on host (still proxy-measured)."
                )
            else:
                emit("Docker not available — running on host (still proxy-measured).")
        else:
            emit(
                "Container isolation disabled — running on host (still proxy-measured)."
            )
        config["_use_docker"] = use_docker

        description = describe_experiment(config, use_docker)
        emit(description)

        campaigns = []
        for repo in config["repos"]:
            try:
                campaigns.append(await self._campaign(repo, config, emit))
            except Exception as exc:  # one repo failing must not kill the experiment
                # Surface the REAL cause: str(exc) alone can be opaque (e.g. a
                # bare "1" from KeyError(1)).  Log the full traceback to the
                # journal and store type+message so failures aren't mysterious.
                log.exception("[%s] campaign failed", repo)
                tb = traceback.format_exc()
                detail = f"{type(exc).__name__}: {exc}"
                emit(f"[{repo}] FAILED: {detail}")
                campaigns.append(
                    {
                        "repo": repo,
                        "files": 0,
                        "timeline": [],
                        "summary": {},
                        "error": detail,
                        "traceback": tb,
                    }
                )
        return {"description": description, "campaigns": campaigns}


def describe_experiment(config: dict, use_docker: bool) -> str:
    """Human-readable summary of precisely what an experiment run does.

    Stored at the top of each experiment history so you can tell at a glance what
    was measured, without decoding the raw config.
    """
    repos = config.get("repos", []) or []
    methods = config.get("methods", []) or []
    passes = int(config.get("passes", 0) or 0)
    pct = config.get("pct", 0)
    seed = config.get("seed", 0)
    human_commits = int(config.get("human_commits", 0) or 0)
    hook_warms = bool(config.get("hook_warms", False))
    cold_bundle = bool(config.get("cold_bundle", False))

    isolation = (
        "each agent pass runs in a fresh, disposable Docker container"
        if use_docker
        else "agent passes run on the host (no container isolation)"
    )
    if human_commits:
        warm_method = config.get("warm_method", "hook")
        if hook_warms:
            human = (
                f"{human_commits} teammate (human) commit(s) interleaved between agent "
                f"passes, cache warmed after each human commit via the {warm_method!r} "
                f"mechanism (hook = in-process server post-receive; "
                f"action = github-action subprocess incl. blobless bundle; "
                f"both = run and compare); the next agent stays warm"
            )
        else:
            human = (
                f"{human_commits} teammate (human) commit(s) interleaved between agent "
                "passes, with no hook warming (the next agent goes cold)"
            )
    else:
        human = "no human commits"

    if cold_bundle:
        cold_note = (
            "COLD agentgitsmart passes use a pre-built blobless bundle (production-amortised "
            "cold: the bundle is built once per commit and reused, as a CDN would serve it)"
        )
    else:
        cold_note = (
            "COLD is the honest un-amortised first-visit cost (no pre-built bundle; "
            "full history flows through the git daemon exactly as a real first agent pays)"
        )

    return (
        f"Cache experiment over {len(repos)} repo(s) "
        f"[{', '.join(repos) if repos else '-'}]: "
        f"{passes} agent pass(es) per repo (pass 1 = COLD/builds the cache, "
        f"later passes = WARM/steady state); methods compared: "
        f"{', '.join(methods) if methods else '-'}; the agent edits {pct}% of "
        f"source files each pass (seed {seed}); {human}; {isolation}; {cold_note}. "
        f"Agent network cost is measured end-to-end through a byte-counting proxy; "
        f"each human step records its own wall time and the cache-rebuild load it "
        f"triggered (delta vs full, files reindexed/carried-forward, bytes materialized)."
    )


def _summarize_campaign(timeline: list[dict], methods: list[str]) -> dict:
    """Per-method cold (1st) vs warm (avg of rest) bytes + wall, and win vs naive."""
    agent_passes = [p for p in timeline if p["kind"] == "agent"]
    out: dict = {}
    for m in methods:
        cells = [
            p["cells"][m]
            for p in agent_passes
            if m in p["cells"] and not p["cells"][m]["error"]
        ]
        if not cells:
            out[m] = {
                "cold_bytes": None,
                "warm_avg_bytes": None,
                "cold_wall": None,
                "warm_avg_wall": None,
                "runs": 0,
            }
            continue
        cold = cells[0]
        warm = cells[1:] or cells  # if only one pass, warm == that pass
        out[m] = {
            "cold_bytes": cold["bytes"],
            "warm_avg_bytes": round(sum(c["bytes"] for c in warm) / len(warm)),
            "cold_wall": cold["wall_s"],
            "warm_avg_wall": round(sum(c["wall_s"] for c in warm) / len(warm), 3),
            "cold_roundtrips": cold.get("roundtrips", 0),
            "warm_avg_roundtrips": round(
                sum(c.get("roundtrips", 0) for c in warm) / len(warm), 1
            ),
            "cold_bundle_used": cold.get("bundle_used", False),
            "warm_bundle_used": warm[0].get("bundle_used", False),
            "cold_clone_ms": cold.get("phase_clone_ms", 0.0),
            "warm_avg_clone_ms": round(
                sum(c.get("phase_clone_ms", 0.0) for c in warm) / len(warm), 1
            ),
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
        wins_wall[m] = (
            round(naive_warm_wall / mwall, 1) if (naive_warm_wall and mwall) else None
        )
    out["_win_vs_naive"] = wins
    out["_win_vs_naive_wall"] = wins_wall
    return out
