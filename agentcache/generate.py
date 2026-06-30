"""agentcache.generate — packaged CLI entrypoint for generating the agent-cache side ref.

This module holds the core of the generate CLI so it can be installed as a
console script (``agentcache-generate``) and invoked from CI without requiring
the AgentCache source tree to be on ``sys.path``.

Output contract (preserved from scripts/generate_agentcache.py):
  source_commit : <sha>
  cache_ref     : refs/agent-cache/<sha>
  cache_commit  : <orphan-sha>
  manifest      : N entries
  symbols       : N (ctags=yes|no)
  [bundle        : <path>]  # only when --bundle-dir is set
  ::AGENTCACHE_REF::refs/agent-cache/<sha>   ← machine-readable trailer CI greps

The trailing ``::AGENTCACHE_REF::`` line is the signal CI uses to push the ref
back to the remote.  Do not remove or reformat it.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

import pygit2

from agentcache.config import AgentCacheConfig
from agentcache import hook as hook_mod
from agentcache import bundle as bundle_mod


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.  Returns exit code (0 on success, non-zero on error).

    Args:
        argv: Argument list to parse.  Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code integer.  Always 0 on success.

    Output (stdout):
        source_commit, cache_ref, cache_commit, manifest count, symbol count,
        optional bundle path, and the canonical ``::AGENTCACHE_REF::<ref>``
        machine-readable marker.
    """
    p = argparse.ArgumentParser(
        description=(
            "Generate the agentcache side ref for one commit.\n\n"
            "Builds manifest.json + symbols.json (+ meta/agents.md) for the "
            "commit and stores them as an orphan commit under "
            "refs/agent-cache/<commit-oid>.  Push that ref back to the remote "
            "and agents can fetch the cache directly -- no running query "
            "service required."
        )
    )
    p.add_argument("--repo", default=".", help="Path to the repo (default: cwd)")
    p.add_argument("--commit", default="HEAD", help="Commit to cache (default: HEAD)")
    p.add_argument(
        "--bundle-dir",
        default=None,
        help="If set, also write a blobless bootstrap bundle here.",
    )
    p.add_argument(
        "--branch",
        default=None,
        help="Branch ref for the bundle (default: current HEAD branch).",
    )
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
    print(
        f"symbols       : {meta.get('symbol_count', '?')} "
        f"(ctags={'yes' if meta.get('ctags_available') else 'no'})"
    )

    if args.bundle_dir:
        os.makedirs(args.bundle_dir, exist_ok=True)
        branch = args.branch or repo.head.shorthand
        out = os.path.join(args.bundle_dir, f"{commit}.bundle")
        bundle_mod.create_blobless_bundle(
            repo,
            f"refs/heads/{branch}",
            out,
            filter_spec=cfg.bundle_filter,
        )
        print(f"bundle        : {out}")

    # Emit the ref name on a trailing line so CI can `git push` it.
    print(f"::AGENTCACHE_REF::{result['cache_ref']}")
    return 0
