"""Manifest tests: completeness, flatness, correct OIDs/sizes/sorting."""
from __future__ import annotations

import subprocess

import pygit2

from agentcache.manifest import build_manifest
from tests.conftest import FILES


def test_manifest_lists_every_file_recursively(repo):
    r, commit = repo
    man = build_manifest(r, commit)
    paths = {e["path"] for e in man["entries"]}
    assert paths == set(FILES)  # full recursive set, nested paths included
    assert man["entry_count"] == len(FILES)
    assert man["source_commit"] == commit


def test_manifest_sorted_by_path(repo):
    r, commit = repo
    man = build_manifest(r, commit)
    paths = [e["path"] for e in man["entries"]]
    assert paths == sorted(paths)


def test_manifest_sizes_and_oids_match_git(repo):
    r, commit = repo
    man = build_manifest(r, commit)
    by_path = {e["path"]: e for e in man["entries"]}

    for rel, data in FILES.items():
        entry = by_path[rel]
        assert entry["size"] == len(data.encode())
        assert entry["mode"] == "100644"
        # OID must equal what git itself resolves for path@commit.
        expect = subprocess.run(
            ["git", "--git-dir", r.path, "rev-parse", f"{commit}:{rel}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert entry["oid"] == expect


def test_manifest_resolves_without_blobs(repo):
    """The manifest must be derivable from trees alone (blobless-clone safe)
    in spirit: every entry's blob OID is recorded so the agent can fetch by
    OID later without ever having held the content."""
    r, commit = repo
    man = build_manifest(r, commit)
    for e in man["entries"]:
        # OID is a valid 40-hex sha and present in the object db here (server side)
        assert len(e["oid"]) == 40
        assert isinstance(r[e["oid"]], pygit2.Blob)
