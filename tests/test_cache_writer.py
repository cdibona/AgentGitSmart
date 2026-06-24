"""Cache-writer tests: orphan commit, side ref, readback, idempotency."""
from __future__ import annotations

import pygit2
import pytest

from agentcache import cache_writer


def test_write_creates_orphan_commit_under_side_ref(repo):
    r, commit = repo
    res = cache_writer.write_cache(
        r, commit, {"manifest.json": b'{"k":1}', "meta.json": b"{}"}
    )
    ref = res["cache_ref"]
    assert ref == f"refs/agent-cache/{commit}"
    assert ref in r.references
    # Orphan: the cache commit has no parents and is not on any branch history.
    cache_commit = r[res["cache_commit"]]
    assert list(cache_commit.parent_ids) == []
    # It is distinct from the source commit and does not reference it.
    assert res["cache_commit"] != commit


def test_readback_roundtrip(repo):
    r, commit = repo
    payload = b'{"entries": []}'
    cache_writer.write_cache(r, commit, {"manifest.json": payload})
    got = cache_writer.read_artifact(r, commit, "manifest.json")
    assert got == payload


def test_read_missing_artifact_raises(repo):
    r, commit = repo
    cache_writer.write_cache(r, commit, {"manifest.json": b"{}"})
    with pytest.raises(KeyError):
        cache_writer.read_artifact(r, commit, "nope.json")


def test_read_missing_ref_raises(repo):
    r, commit = repo
    with pytest.raises(KeyError):
        cache_writer.read_artifact(r, "0" * 40, "manifest.json")


def test_regeneration_is_idempotent(repo):
    r, commit = repo
    first = cache_writer.write_cache(r, commit, {"manifest.json": b"v1"})
    second = cache_writer.write_cache(r, commit, {"manifest.json": b"v2"})
    # Same ref, force-updated to the newer cache commit; content reflects v2.
    assert first["cache_ref"] == second["cache_ref"]
    assert cache_writer.read_artifact(r, commit, "manifest.json") == b"v2"


def test_list_caches(repo):
    r, commit = repo
    assert cache_writer.list_caches(r) == []
    cache_writer.write_cache(r, commit, {"manifest.json": b"{}"})
    assert cache_writer.list_caches(r) == [commit]


def test_rejects_nested_artifact_names(repo):
    r, commit = repo
    with pytest.raises(ValueError):
        cache_writer.write_cache(r, commit, {"a/b.json": b"{}"})
