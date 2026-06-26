"""Blobless bootstrap bundles for CDN-cacheable cold starts.

A fleet of fresh VMs each negotiating a filtered clone hammers the server's
reachability/filter evaluation. Precomputing a static blobless bundle per HEAD
commit turns the common path into "download a cached file from object storage."

IMPORTANT consumption detail (verified against git 2.43): you cannot
``git clone`` a filtered bundle directly -- git refuses with
"cannot clone from filtered bundle". Filtered bundles are consumed via the
*bundle-uri* mechanism, i.e. the VM clones from the real promisor *with*
``--bundle-uri`` pointing at this file:

    git clone --filter=blob:none --bundle-uri=<url-or-path> <promisor-url> repo

git ingests the bundle first (seeding commits + trees), then the catch-up fetch
from the promisor transfers almost nothing.
"""

from __future__ import annotations

import os
import subprocess

import pygit2


def create_blobless_bundle(
    repo: pygit2.Repository,
    rev: str,
    out_path: str,
    *,
    filter_spec: str = "blob:none",
) -> str:
    """Write a filtered bundle for ``rev`` to ``out_path``; return the path.

    ``rev`` should be a ref (e.g. ``refs/heads/main``) so the bundle carries a
    ref the client can clone/seed against, not a bare commit OID.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    subprocess.run(
        [
            "git",
            "--git-dir",
            repo.path,
            "bundle",
            "create",
            out_path,
            f"--filter={filter_spec}",
            rev,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out_path


def verify_bundle(repo: pygit2.Repository, path: str) -> str:
    """Return ``git bundle verify`` output (mentions the object filter)."""
    proc = subprocess.run(
        ["git", "--git-dir", repo.path, "bundle", "verify", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr
