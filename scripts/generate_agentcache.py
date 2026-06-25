#!/usr/bin/env python3
"""Generate the agentcache side ref for one commit (default: HEAD).

This is what the post-receive hook does on a self-hosted server, packaged so it
can run anywhere a server hook can't — most importantly inside CI (GitHub
Actions) so a GitHub-hosted repo can be agentcache-enabled without a git server.

It builds manifest.json + symbols.json (+ meta/agents.md) for the commit and
stores them as an orphan commit under refs/agent-cache/<commit-oid>.  Push that
ref back to the remote and agents can fetch the cache directly from the side ref
— no running query service required.

Usage:
    python scripts/generate_agentcache.py [--repo .] [--commit HEAD] [--bundle-dir DIR]
"""
from __future__ import annotations

import argparse
import os
import sys

import pygit2

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcache.config import AgentCacheConfig  # noqa: E402
from agentcache import hook as hook_mod  # noqa: E402
from agentcache import bundle as bundle_mod  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=".", help="Path to the repo (default: cwd)")
    p.add_argument("--commit", default="HEAD", help="Commit to cache (default: HEAD)")
    p.add_argument("--bundle-dir", default=None,
                   help="If set, also write a blobless bootstrap bundle here.")
    p.add_argument("--branch", default=None,
                   help="Branch ref for the bundle (default: current HEAD branch).")
    args = p.parse_args(argv)

    repo = pygit2.Repository(args.repo)
    commit = str(repo.revparse_single(args.commit).id)
    cfg = AgentCacheConfig(repo_dir=args.repo, bundle_dir=args.bundle_dir)

    result = hook_mod.generate_for_commit(repo, commit, cfg)
    meta = result.get("meta", {})
    print(f"source_commit : {commit}")
    print(f"cache_ref     : {result['cache_ref']}")
    print(f"cache_commit  : {result['cache_commit']}")
    print(f"manifest      : {meta.get('manifest_entries', '?')} entries")
    print(f"symbols       : {meta.get('symbol_count', '?')} "
          f"(ctags={'yes' if meta.get('ctags_available') else 'no'})")

    if args.bundle_dir:
        os.makedirs(args.bundle_dir, exist_ok=True)
        branch = args.branch or repo.head.shorthand
        out = os.path.join(args.bundle_dir, f"{commit}.bundle")
        bundle_mod.create_blobless_bundle(repo, f"refs/heads/{branch}", out,
                                          filter_spec=cfg.bundle_filter)
        print(f"bundle        : {out}")

    # Emit the ref name on a trailing line so CI can `git push` it.
    print(f"::AGENTCACHE_REF::{result['cache_ref']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
