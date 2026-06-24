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
from functools import lru_cache
from typing import Any, Dict, List

import pygit2
from flask import Flask, jsonify, request

from .config import AgentCacheConfig
from . import cache_writer


def create_app(cfg: AgentCacheConfig) -> Flask:
    app = Flask(__name__)
    repo = pygit2.Repository(cfg.repo_dir)

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

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", repo=cfg.repo_dir)

    @app.get("/caches")
    def caches():
        return jsonify(commits=cache_writer.list_caches(repo, cfg.ref_prefix))

    @app.get("/cache/<commit>/manifest")
    def manifest(commit: str):
        try:
            raw = cache_writer.read_artifact(
                repo, commit, "manifest.json", ref_prefix=cfg.ref_prefix
            )
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        return app.response_class(raw, mimetype="application/json")

    @app.get("/cache/<commit>/symbol/<name>")
    def symbol(commit: str, name: str):
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
            fetch_oids=sorted({l["oid"] for l in locations if "oid" in l}),
        )

    @app.post("/cache/<commit>/resolve")
    def resolve(commit: str):
        body = request.get_json(silent=True) or {}
        paths: List[str] = body.get("paths", [])
        try:
            idx = _manifest_index(commit)
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        resolved, missing = [], []
        for p in paths:
            entry = idx.get(p)
            if entry is None:
                missing.append(p)
            else:
                resolved.append(
                    {
                        "path": p,
                        "oid": entry["oid"],
                        "size": entry["size"],
                        "mode": entry["mode"],
                    }
                )
        return jsonify(
            commit=commit,
            resolved=resolved,
            missing=missing,
            # Hand the agent exactly what to put after `git fetch origin ...`
            fetch_oids=[r["oid"] for r in resolved],
            total_bytes=sum(r["size"] or 0 for r in resolved),
        )

    @app.get("/cache/<commit>/agents.md")
    def agents_md(commit: str):
        try:
            raw = cache_writer.read_artifact(repo, commit, "agents.md", ref_prefix=cfg.ref_prefix)
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        return app.response_class(raw, mimetype="text/markdown; charset=utf-8")

    @app.get("/agents.md")
    def agents_md_latest():
        commits = cache_writer.list_caches(repo, cfg.ref_prefix)
        if not commits:
            return jsonify(error="no caches exist yet; run the post-receive hook to generate one"), 404
        latest = commits[-1]
        try:
            raw = cache_writer.read_artifact(repo, latest, "agents.md", ref_prefix=cfg.ref_prefix)
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        return app.response_class(raw, mimetype="text/markdown; charset=utf-8")

    return app


def main() -> int:  # pragma: no cover - thin runner
    cfg = AgentCacheConfig.from_env()
    app = create_app(cfg)
    app.run(host=cfg.service_host, port=cfg.service_port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
