"""Prepare every mirrored repo for the experiments — idempotent.

For each repo under benchmark/repos/<name>.git this:
  1. enables uploadpack.allowFilter + allowAnySHA1InWant (needed for blobless
     clones and by-OID blob fetches), and
  2. generates a blobless bootstrap bundle benchmark/bundles/<name>.git-<branch>.bundle
     if missing (lets agentgitsmart seed history from a local/CDN file instead of
     the git server).

Adding a project to the fleet is therefore just:

    git clone --mirror <url> benchmark/repos/<name>.git
    python -m experiments.prep            # configure + bundle everything new

Safe to re-run; existing bundles are left alone unless --force.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.harness import discover_repos, REPOS_DIR  # noqa: E402

BUNDLES_DIR = _ROOT / "benchmark" / "bundles"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def default_branch(repo_dir: str) -> str:
    out = _git("--git-dir", repo_dir, "symbolic-ref", "--short", "HEAD")
    return out.stdout.strip() or "main"


def configure(repo_dir: str) -> None:
    _git("--git-dir", repo_dir, "config", "uploadpack.allowFilter", "true")
    _git("--git-dir", repo_dir, "config", "uploadpack.allowAnySHA1InWant", "true")


def ensure_bundle(name: str, repo_dir: str, *, force: bool = False) -> tuple[str, str]:
    """Generate <name>.git-<branch>.bundle if missing. Returns (status, path)."""
    branch = default_branch(repo_dir)
    BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    out = BUNDLES_DIR / f"{name}.git-{branch}.bundle"
    if out.exists() and not force:
        return ("exists", str(out))
    proc = _git(
        "--git-dir",
        repo_dir,
        "bundle",
        "create",
        str(out),
        "--filter=blob:none",
        f"refs/heads/{branch}",
    )
    if proc.returncode != 0:
        return (
            "FAILED: " + proc.stderr.strip().splitlines()[-1]
            if proc.stderr
            else "FAILED",
            str(out),
        )
    return ("created", str(out))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repos",
        nargs="*",
        default=None,
        help="Subset of repo names (default: all discovered).",
    )
    p.add_argument(
        "--force", action="store_true", help="Regenerate bundles even if present."
    )
    args = p.parse_args(argv)

    repos = args.repos or discover_repos()
    if not repos:
        print("No repos found under benchmark/repos/.")
        return 1

    print(f"Preparing {len(repos)} repo(s):")
    for name in repos:
        repo_dir = str(Path(REPOS_DIR) / f"{name}.git")
        if not Path(repo_dir).is_dir():
            print(f"  {name:24} MISSING ({repo_dir})")
            continue
        configure(repo_dir)
        branch = default_branch(repo_dir)
        status, path = ensure_bundle(name, repo_dir, force=args.force)
        # quick scale read: files at HEAD
        n = _git("--git-dir", repo_dir, "ls-tree", "-r", "--name-only", "HEAD")
        nfiles = len(n.stdout.splitlines()) if n.returncode == 0 else "?"
        print(f"  {name:24} branch={branch:16} files={nfiles:>7}  bundle={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
