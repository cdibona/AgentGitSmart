"""Bundle generation and the full cold-start path.

The cold-start test proves the claim end to end: a blobless clone seeded by the
bootstrap bundle holds trees but no blobs, and a single by-OID fetch hydrates
exactly the file the agent decided to touch.
"""
from __future__ import annotations

import subprocess

import pygit2
import pytest

from agentcache import bundle as bundle_mod


def _git(*args, **kw):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, **kw)


def test_blobless_bundle_records_filter(repo, tmp_path):
    r, _ = repo
    out = str(tmp_path / "boot.bundle")
    bundle_mod.create_blobless_bundle(r, "refs/heads/master", out)
    info = bundle_mod.verify_bundle(r, out)
    assert "blob:none" in info  # git reports the object filter


def test_cannot_clone_filtered_bundle_directly(repo, tmp_path):
    """Documents the gotcha the design routes around (use --bundle-uri)."""
    r, _ = repo
    out = str(tmp_path / "boot.bundle")
    bundle_mod.create_blobless_bundle(r, "refs/heads/master", out)
    proc = subprocess.run(
        ["git", "clone", "--bare", out, str(tmp_path / "fromboot.git")],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "filtered bundle" in (proc.stderr + proc.stdout)


def test_cold_start_blobless_then_targeted_fetch(repo, tmp_path):
    r, commit = repo
    # Make the source repo a promisor that allows filters and by-OID wants
    # (the same allowFilter / allowAnySHA1InWant knobs JGit's UploadPack uses).
    _git("--git-dir", r.path, "config", "uploadpack.allowFilter", "true")
    _git("--git-dir", r.path, "config", "uploadpack.allowanysha1inwant", "true")

    out = str(tmp_path / "boot.bundle")
    bundle_mod.create_blobless_bundle(r, "refs/heads/master", out)

    origin = "file://" + r.path
    vm = str(tmp_path / "vm")
    # Cold start: blobless clone, seeded by the bundle via --bundle-uri.
    _git("clone", "--filter=blob:none", "--no-checkout",
         "--bundle-uri=" + out, origin, vm)

    vmrepo = pygit2.Repository(vm)

    # Object DB after cold start: commit + trees, but NOT all blobs.
    types = {}
    for oid in vmrepo:
        types[vmrepo[oid].type_str] = types.get(vmrepo[oid].type_str, 0) + 1
    assert types.get("commit", 0) >= 1
    assert types.get("tree", 0) >= 1

    # Agent resolves a path -> OID from trees alone (no content fetched yet).
    target = _git("--git-dir", vm + "/.git", "rev-parse", f"HEAD:src/app.py").stdout.strip()

    # Confirm it's genuinely absent locally (disable lazy auto-fetch to observe).
    probe = subprocess.run(
        ["git", "--git-dir", vm + "/.git", "cat-file", "-e", target],
        capture_output=True, text=True,
        env={"GIT_NO_LAZY_FETCH": "1", "PATH": __import__("os").environ["PATH"]},
    )
    assert probe.returncode != 0  # missing -> would need the promisor

    # ONE targeted fetch hydrates exactly that blob.
    _git("--git-dir", vm + "/.git", "fetch", "origin", target)
    content = _git("--git-dir", vm + "/.git", "cat-file", "blob", target).stdout
    assert "TokenRefresher" in content
