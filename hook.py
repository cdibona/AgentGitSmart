"""post-receive entrypoint: generate cache artifacts for each pushed branch.

Wire it up with a thin shell shim (see hooks/post-receive) that execs
``python -m agentcache.hook``. Stdin gets one line per updated ref:

    <old-oid> <new-oid> <refname>

We generate for branch tips (refs/heads/*), skip deletions, and emit a
blobless bootstrap bundle if AGENTCACHE_BUNDLE_DIR is set.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

import pygit2

from . import GENERATOR_VERSION
from .config import AgentCacheConfig
from . import bundle as bundle_mod
from . import cache_writer
from . import manifest as manifest_mod
from . import symbols as symbols_mod

ZERO_OID = "0" * 40


def generate_for_commit(
    repo: pygit2.Repository, commit_oid: str, cfg: AgentCacheConfig
) -> Dict[str, Any]:
    """Build artifacts for one commit, store under the side ref, return summary."""
    man = manifest_mod.build_manifest(repo, commit_oid)
    syms = symbols_mod.build_symbol_index(repo, commit_oid, ctags_bin=cfg.ctags_bin)
    meta = {
        "schema": 1,
        "generator_version": GENERATOR_VERSION,
        "source_commit": str(commit_oid),
        "generated_at": man["generated_at"],
        "manifest_entries": man["entry_count"],
        "symbol_count": syms["symbol_count"],
        "ctags_available": syms["ctags_available"],
    }
    artifacts = {
        "manifest.json": json.dumps(man, separators=(",", ":")).encode(),
        "symbols.json": json.dumps(syms, separators=(",", ":")).encode(),
        "meta.json": json.dumps(meta, indent=2).encode(),
    }
    result = cache_writer.write_cache(
        repo,
        commit_oid,
        artifacts,
        ref_prefix=cfg.ref_prefix,
        bot_name=cfg.bot_name,
        bot_email=cfg.bot_email,
    )
    result["meta"] = meta
    return result


def _handle_ref(repo, old_oid, new_oid, refname, cfg) -> Dict[str, Any] | None:
    if new_oid == ZERO_OID:
        return None  # branch deletion
    if not refname.startswith("refs/heads/"):
        return None  # only branch tips
    summary = generate_for_commit(repo, new_oid, cfg)
    summary["ref"] = refname
    if cfg.bundle_dir:
        out = os.path.join(cfg.bundle_dir, f"{new_oid}.bundle")
        bundle_mod.create_blobless_bundle(
            repo, refname, out, filter_spec=cfg.bundle_filter
        )
        summary["bundle"] = out
    return summary


def main(argv=None, stdin=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    cfg = AgentCacheConfig.from_env()
    repo = pygit2.Repository(cfg.repo_dir)

    any_done = False
    for line in stdin:
        parts = line.split()
        if len(parts) != 3:
            continue
        old_oid, new_oid, refname = parts
        try:
            summary = _handle_ref(repo, old_oid, new_oid, refname, cfg)
        except Exception as exc:  # never block the push on cache failure
            print(f"agentcache: FAILED for {refname}: {exc}", file=sys.stderr)
            continue
        if summary:
            any_done = True
            print(
                "agentcache: {ref} -> {cache_ref} "
                "({n} files, {s} symbols, ctags={c})".format(
                    ref=summary["ref"],
                    cache_ref=summary["cache_ref"],
                    n=summary["meta"]["manifest_entries"],
                    s=summary["meta"]["symbol_count"],
                    c=summary["meta"]["ctags_available"],
                ),
                file=sys.stderr,
            )
    if not any_done:
        print("agentcache: nothing to do", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
