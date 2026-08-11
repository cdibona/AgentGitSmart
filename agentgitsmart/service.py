"""Flask query service the agent VM talks to instead of downloading indexes.

The whole point: the VM stays tiny. It does not pull the symbol index or
embeddings; it asks this service "where is X" / "resolve these paths" and gets
back blob OIDs + sizes, which it then fetches from the promisor in ONE batch:

    git fetch origin <oid1> <oid2> ...   # one pack, not N round-trips

All reads come straight out of the side ref's objects -- no working tree.
A small per-commit manifest cache avoids re-parsing on every request.
"""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from typing import Any, Dict, List

import pygit2
from flask import Flask, jsonify, request

from .config import AgentGitSmartConfig
from . import cache_writer
from . import hook as hook_mod
from . import GENERATOR_VERSION
from .symbols import SYMBOLS_SCHEMA


def create_app(cfg: AgentGitSmartConfig) -> Flask:
    app = Flask(__name__)
    repo = pygit2.Repository(cfg.repo_dir)

    # ── Lazy generation ────────────────────────────────────────────────────────
    # If a cache is requested for a commit that has none, build it on the
    # spot (first agent pays a one-time cost; everyone after reuses it).
    # Per-commit locks prevent two simultaneous first-agents from racing to
    # double-generate; a guard lock protects the lock registry itself.
    _gen_locks: Dict[str, threading.Lock] = {}
    _gen_locks_guard = threading.Lock()

    def _ensure_cache(commit: str) -> bool:
        """Ensure a cache ref exists for *commit*; build it if missing.

        Returns True if a cache is available afterward, False if the commit
        is unknown to this repo or lazy generation is disabled.
        """
        ref = f"{cfg.ref_prefix.rstrip('/')}/{commit}"
        if ref in repo.references:
            # Cache exists — check it's still fresh before serving.
            _ensure_fresh(commit)
            return True
        if not cfg.lazy_generation:
            return False
        # Resolve the commit; if it isn't in this repo we can't generate.
        try:
            target = repo.revparse_single(commit).peel(pygit2.Commit)
        except Exception:
            return False
        commit_hex = str(target.id)
        canonical_ref = f"{cfg.ref_prefix.rstrip('/')}/{commit_hex}"

        with _gen_locks_guard:
            lock = _gen_locks.setdefault(commit_hex, threading.Lock())
        with lock:
            # Re-check inside the lock: another request may have built it.
            if canonical_ref in repo.references:
                return True
            hook_mod.generate_for_commit(repo, commit_hex, cfg)
            # Drop any stale memoized miss for this commit.
            _manifest_index.cache_clear()
        return True

    @lru_cache(maxsize=32)
    def _manifest_index(commit: str) -> Dict[str, Dict[str, Any]]:
        raw = cache_writer.read_artifact(
            repo, commit, "manifest.json", ref_prefix=cfg.ref_prefix
        )
        man = json.loads(raw)
        return {e["path"]: e for e in man["entries"]}

    def _symbols(commit: str) -> Dict[str, Any]:
        raw = cache_writer.read_artifact(
            repo, commit, "symbols.json", ref_prefix=cfg.ref_prefix
        )
        return json.loads(raw)

    # ── Staleness helpers ──────────────────────────────────────────────────────

    def _ensure_fresh(commit: str) -> None:
        """Re-check manifest/symbols versions; force-regenerate if stale.

        Reads the cached manifest's ``generator_version`` and the symbols'
        ``schema``/``generator_version``.  If either is stale (mismatch with
        the current constants), regenerates the cache and clears the LRU.
        Safe and idempotent — never loops; exceptions are suppressed so
        staleness checks never break serving.
        """
        try:
            raw_man = cache_writer.read_artifact(
                repo, commit, "manifest.json", ref_prefix=cfg.ref_prefix
            )
            man_data = json.loads(raw_man)
            stale = man_data.get("generator_version") != GENERATOR_VERSION
            if not stale:
                # Cheap short-circuit: only read symbols if manifest is current.
                raw_sym = cache_writer.read_artifact(
                    repo, commit, "symbols.json", ref_prefix=cfg.ref_prefix
                )
                sym_data = json.loads(raw_sym)
                stale = (
                    sym_data.get("schema") != SYMBOLS_SCHEMA
                    or sym_data.get("generator_version") != GENERATOR_VERSION
                )
            if stale:
                hook_mod.generate_for_commit(repo, commit, cfg)
                _manifest_index.cache_clear()
        except Exception:
            pass  # Never break serving due to freshness-check errors.

    def _any_path_in_tree(commit: str, paths: List[str]) -> bool:
        """Return True if ANY of *paths* exists in the real git tree for *commit*.

        Uses pygit2's ``<rev>:<path>`` revparse syntax — cheap, no blob fetch.
        Short-circuits on the first hit.
        """
        for path in paths:
            try:
                repo.revparse_single(f"{commit}:{path}")
                return True
            except Exception:
                continue
        return False

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", repo=cfg.repo_dir)

    @app.get("/caches")
    def caches():
        return jsonify(commits=cache_writer.list_caches(repo, cfg.ref_prefix))

    @app.get("/cache/<commit>/manifest")
    def manifest(commit: str):
        if not _ensure_cache(commit):
            return jsonify(error=f"commit {commit} unknown to this repo"), 404
        try:
            raw = cache_writer.read_artifact(
                repo, commit, "manifest.json", ref_prefix=cfg.ref_prefix
            )
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        return app.response_class(raw, mimetype="application/json")

    @app.get("/cache/<commit>/symbol/<name>")
    def symbol(commit: str, name: str):
        if not _ensure_cache(commit):
            return jsonify(error=f"commit {commit} unknown to this repo"), 404
        try:
            syms = _symbols(commit)
            idx = _manifest_index(commit)
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        locations = []
        for loc in syms["symbols"].get(name, []):
            entry = idx.get(loc["path"])
            merged = dict(loc)
            if entry:
                merged["oid"] = entry["oid"]
                merged["size"] = entry["size"]
            locations.append(merged)
        return jsonify(
            name=name,
            commit=commit,
            ctags_available=syms.get("ctags_available", False),
            locations=locations,
            fetch_oids=sorted({loc["oid"] for loc in locations if "oid" in loc}),
        )

    @app.post("/cache/<commit>/resolve")
    def resolve(commit: str):
        body = request.get_json(silent=True) or {}
        paths: List[str] = body.get("paths", [])
        if not _ensure_cache(commit):
            return jsonify(error=f"commit {commit} unknown to this repo"), 404
        try:
            idx = _manifest_index(commit)
        except KeyError as exc:
            return jsonify(error=str(exc)), 404

        def _resolve_against(index, path_list):
            res, mis = [], []
            for p in path_list:
                entry = index.get(p)
                if entry is None:
                    mis.append(p)
                else:
                    res.append(
                        {
                            "path": p,
                            "oid": entry["oid"],
                            "size": entry["size"],
                            "mode": entry["mode"],
                        }
                    )
            return res, mis

        resolved, missing = _resolve_against(idx, paths)
        rebuilt = False

        # ── Taint probe ───────────────────────────────────────────────────────
        # If any "missing" path actually exists in the commit's real git tree,
        # the manifest is stale/corrupt for this commit.  Rebuild ONCE, then
        # re-resolve the full original path list against the fresh manifest.
        # Short-circuit: at most one rebuild per request.
        if missing and _any_path_in_tree(commit, missing):
            try:
                target = repo.revparse_single(commit).peel(pygit2.Commit)
                hook_mod.generate_for_commit(repo, str(target.id), cfg)
                _manifest_index.cache_clear()
                fresh_idx = _manifest_index(commit)
                resolved, missing = _resolve_against(fresh_idx, paths)
                rebuilt = True
            except Exception:
                pass  # defensive: keep original results on any failure

        return jsonify(
            commit=commit,
            resolved=resolved,
            missing=missing,
            # Hand the agent exactly what to put after `git fetch origin ...`
            fetch_oids=[r["oid"] for r in resolved],
            total_bytes=sum(r["size"] or 0 for r in resolved),
            rebuilt=rebuilt,
        )

    @app.get("/cache/<commit>/agents.md")
    def agents_md(commit: str):
        try:
            raw = cache_writer.read_artifact(
                repo, commit, "agents.md", ref_prefix=cfg.ref_prefix
            )
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        return app.response_class(raw, mimetype="text/markdown; charset=utf-8")

    @app.get("/agents.md")
    def agents_md_latest():
        commits = cache_writer.list_caches(repo, cfg.ref_prefix)
        if not commits:
            return jsonify(
                error="no caches exist yet; run the post-receive hook to generate one"
            ), 404
        latest = commits[-1]
        try:
            raw = cache_writer.read_artifact(
                repo, latest, "agents.md", ref_prefix=cfg.ref_prefix
            )
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        return app.response_class(raw, mimetype="text/markdown; charset=utf-8")

    return app


def main() -> int:  # pragma: no cover - thin runner
    cfg = AgentGitSmartConfig.from_env()
    app = create_app(cfg)
    app.run(host=cfg.service_host, port=cfg.service_port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
