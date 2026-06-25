"""Erase every trace of agentcache from a repository.

agentcache is deliberately low-footprint: it stores its artifacts as orphan
commits under ``refs/agent-cache/*`` — out of the main history, invisible to
``git log`` / ``git branch`` / normal clones.  Nothing in your working tree
or commit history is ever touched.

Still, for testing and user comfort, this tool removes the side refs (and,
optionally, the post-receive hook and any generated bundles) and reclaims the
now-unreachable objects with ``git gc``.  After it runs, the repository is
byte-for-byte indistinguishable from one that never had agentcache.

Usage::

    python -m agentcache.uninstall --repo /srv/git/myrepo.git           # dry run
    python -m agentcache.uninstall --repo /srv/git/myrepo.git --yes      # do it
    python -m agentcache.uninstall --repo ... --yes --remove-hook --gc
    python -m agentcache.uninstall --repo ... --yes --bundles /srv/bundles
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional

import pygit2

from .cache_writer import list_caches


def find_cache_refs(repo: pygit2.Repository, ref_prefix: str = "refs/agent-cache") -> List[str]:
    """Return the full names of all agent-cache side refs in *repo*."""
    prefix = ref_prefix.rstrip("/") + "/"
    return sorted(name for name in repo.references if name.startswith(prefix))


def delete_cache_refs(repo: pygit2.Repository, ref_prefix: str = "refs/agent-cache") -> int:
    """Delete every agent-cache side ref. Returns the count removed."""
    refs = find_cache_refs(repo, ref_prefix)
    for name in refs:
        repo.references[name].delete()
    return len(refs)


def _hook_is_agentcache(hook_path: str) -> bool:
    """True if the post-receive hook references agentcache (safe to remove)."""
    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            return "agentcache" in fh.read()
    except OSError:
        return False


def remove_hook(repo_dir: str) -> Optional[str]:
    """Remove the post-receive hook if it is an agentcache shim. Returns path removed."""
    hook_path = os.path.join(repo_dir, "hooks", "post-receive")
    if os.path.exists(hook_path) and _hook_is_agentcache(hook_path):
        os.remove(hook_path)
        return hook_path
    return None


def remove_bundles(bundle_dir: str) -> int:
    """Delete *.bundle files from bundle_dir. Returns count removed."""
    if not os.path.isdir(bundle_dir):
        return 0
    removed = 0
    for name in os.listdir(bundle_dir):
        if name.endswith(".bundle"):
            os.remove(os.path.join(bundle_dir, name))
            removed += 1
    return removed


def run_gc(repo_dir: str) -> None:
    """Reclaim now-unreachable cache objects with an aggressive prune."""
    subprocess.run(
        ["git", "--git-dir", repo_dir, "gc", "--prune=now", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )


def erase(
    repo_dir: str,
    *,
    ref_prefix: str = "refs/agent-cache",
    remove_hook_too: bool = False,
    bundle_dir: Optional[str] = None,
    gc: bool = False,
    dry_run: bool = True,
) -> dict:
    """Erase agentcache traces from the repo at *repo_dir*.

    With ``dry_run=True`` (default) nothing is changed; the returned dict
    reports what *would* be removed.
    """
    repo = pygit2.Repository(repo_dir)
    refs = find_cache_refs(repo, ref_prefix)
    cached_commits = list_caches(repo, ref_prefix)

    summary: dict = {
        "repo_dir": repo_dir,
        "cache_refs": refs,
        "cache_ref_count": len(refs),
        "cached_commits": cached_commits,
        "hook_removed": None,
        "bundles_removed": 0,
        "gc_run": False,
        "dry_run": dry_run,
    }

    if dry_run:
        return summary

    summary["cache_ref_count"] = delete_cache_refs(repo, ref_prefix)
    if remove_hook_too:
        summary["hook_removed"] = remove_hook(repo_dir)
    if bundle_dir:
        summary["bundles_removed"] = remove_bundles(bundle_dir)
    if gc:
        run_gc(repo_dir)
        summary["gc_run"] = True

    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--repo", required=True, help="Path to the (bare) git repo.")
    p.add_argument("--ref-prefix", default="refs/agent-cache",
                   help="Side-ref namespace to erase (default: refs/agent-cache).")
    p.add_argument("--yes", action="store_true",
                   help="Actually perform the erase (otherwise dry-run).")
    p.add_argument("--remove-hook", action="store_true",
                   help="Also remove the post-receive hook (only if it's an agentcache shim).")
    p.add_argument("--bundles", metavar="DIR", default=None,
                   help="Also delete *.bundle files from this directory.")
    p.add_argument("--gc", action="store_true",
                   help="Run git gc --prune=now to reclaim orphaned objects.")
    args = p.parse_args(argv)

    if not os.path.exists(args.repo):
        print(f"agentcache uninstall: repo not found: {args.repo}", file=sys.stderr)
        return 1

    summary = erase(
        args.repo,
        ref_prefix=args.ref_prefix,
        remove_hook_too=args.remove_hook,
        bundle_dir=args.bundles,
        gc=args.gc,
        dry_run=not args.yes,
    )

    n = summary["cache_ref_count"]
    if summary["dry_run"]:
        print(f"[dry-run] would remove {n} cache ref(s) from {args.repo}")
        for r in summary["cache_refs"]:
            print(f"  - {r}")
        if args.remove_hook:
            print("  - would remove post-receive hook (if agentcache shim)")
        if args.bundles:
            print(f"  - would remove *.bundle from {args.bundles}")
        if args.gc:
            print("  - would run git gc --prune=now")
        print("\nRe-run with --yes to perform the erase.")
    else:
        print(f"Removed {n} cache ref(s) from {args.repo}")
        if summary["hook_removed"]:
            print(f"Removed hook: {summary['hook_removed']}")
        if summary["bundles_removed"]:
            print(f"Removed {summary['bundles_removed']} bundle file(s)")
        if summary["gc_run"]:
            print("Ran git gc --prune=now")
        print("agentcache traces erased.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
