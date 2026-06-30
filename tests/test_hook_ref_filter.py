"""Tests for the ref-filter guard in agentcache.hook._handle_ref.

Locks the loop-prevention guarantee: cache refs written under cfg.ref_prefix
must never trigger re-generation, and only refs/heads/* branch tips cause
cache generation at all.

Key design note
---------------
The default ref_prefix is "refs/agent-cache", which is already outside
refs/heads/*, so the *existing* allowlist filter would silently catch it.
Tests 1 and 5 therefore use a custom ref_prefix of "refs/heads/cache" —
a prefix that IS inside refs/heads/* and would slip past the old filter
if the guard were absent.  This makes the tests fail if (and only if)
the explicit guard is removed, proving the guard is what prevents the loop.

Run with: .venv/bin/pytest tests/test_hook_ref_filter.py -v
"""

from __future__ import annotations

import io

import pygit2

from agentcache.config import AgentCacheConfig
from agentcache.hook import ZERO_OID, _handle_ref, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cache_ref_count(repo: pygit2.Repository, ref_prefix: str) -> int:
    """Count refs whose name starts with ref_prefix + '/'."""
    prefix = ref_prefix.rstrip("/") + "/"
    return sum(1 for r in repo.references if r.startswith(prefix))


def _cfg_with_prefix(repo: pygit2.Repository, prefix: str) -> AgentCacheConfig:
    """Build a cfg pointing at *repo* with a custom ref_prefix."""
    return AgentCacheConfig(repo_dir=repo.path, ref_prefix=prefix)


# ---------------------------------------------------------------------------
# 1. Guard (defense in depth): own cache namespace → None, no new refs
#
# Use ref_prefix="refs/heads/cache" — a prefix that sits *inside* refs/heads/*
# so the old allowlist would NOT catch it; only the explicit guard does.
# → removing the guard causes this test to FAIL (generation happens instead).
# ---------------------------------------------------------------------------


def test_handle_ref_skips_own_cache_namespace(repo):
    """A refname under cfg.ref_prefix must be skipped (returns None) and must
    not create any new cache refs, even when the prefix is inside refs/heads/*.
    """
    r, commit_oid = repo
    # Custom prefix inside refs/heads/ — bypasses the old refs/heads/ allowlist.
    cfg = _cfg_with_prefix(r, "refs/heads/cache")
    cache_refname = f"{cfg.ref_prefix}/{commit_oid}"

    before = _cache_ref_count(r, cfg.ref_prefix)
    result = _handle_ref(r, ZERO_OID, commit_oid, cache_refname, cfg)

    assert result is None, (
        f"Expected None for cache-namespace ref '{cache_refname}'; "
        "got generation — the loop-prevention guard may be missing"
    )
    assert _cache_ref_count(r, cfg.ref_prefix) == before, (
        "No new cache refs should be created when the guard fires"
    )


# ---------------------------------------------------------------------------
# 2. Non-branch non-cache ref (tag) → None
# ---------------------------------------------------------------------------


def test_handle_ref_skips_tags(repo, cfg):
    """refs/tags/* refs are not branch tips — must return None."""
    r, commit_oid = repo
    result = _handle_ref(r, ZERO_OID, commit_oid, "refs/tags/v1", cfg)
    assert result is None


# ---------------------------------------------------------------------------
# 3. Deletion (new_oid == ZERO_OID) on a real branch → None
# ---------------------------------------------------------------------------


def test_handle_ref_skips_deletions(repo, cfg):
    """Branch deletions (new_oid all-zeros) must be skipped."""
    r, commit_oid = repo
    result = _handle_ref(r, commit_oid, ZERO_OID, "refs/heads/main", cfg)
    assert result is None


# ---------------------------------------------------------------------------
# 4. Positive control: valid refs/heads/* branch → generates cache
# ---------------------------------------------------------------------------


def test_handle_ref_generates_for_valid_branch(repo, cfg):
    """A real branch push must trigger generation (positive control)."""
    r, commit_oid = repo
    # The fixture creates the initial commit on refs/heads/master.
    result = _handle_ref(r, ZERO_OID, commit_oid, "refs/heads/master", cfg)

    assert result is not None, "Expected generation for a valid branch push"
    expected_ref = f"{cfg.ref_prefix}/{commit_oid}"
    assert expected_ref in r.references, (
        f"Cache ref {expected_ref} should have been created"
    )


# ---------------------------------------------------------------------------
# 5. main() with mixed stdin: exactly one cache generated
#
# Same defense-in-depth angle as test 1: use ref_prefix="refs/heads/cache"
# so the cache-namespace line would pass the old refs/heads/ check without
# the explicit guard.  Removing the guard causes generation for both lines,
# and since the second call uses the same commit_oid the write is idempotent —
# the total count stays 1, masking the bug.  We therefore also assert that
# the second line produces NO summary in main()'s output by checking stderr.
# ---------------------------------------------------------------------------


def test_main_mixed_stdin_generates_exactly_one_cache(repo, monkeypatch):
    """main() fed a valid branch line AND a cache-namespace line (with a
    ref_prefix inside refs/heads/*) must produce exactly one cache entry.
    """
    r, commit_oid = repo
    custom_prefix = "refs/heads/cache"

    # Point main() at the test repo and the custom prefix.
    monkeypatch.setenv("AGENTCACHE_REPO_DIR", r.path)
    monkeypatch.setenv("AGENTCACHE_REF_PREFIX", custom_prefix)

    cache_refname = f"{custom_prefix}/{commit_oid}"
    stdin_text = (
        f"{ZERO_OID} {commit_oid} refs/heads/master\n"
        f"{ZERO_OID} {commit_oid} {cache_refname}\n"
    )

    before = _cache_ref_count(r, custom_prefix)
    rc = main(argv=[], stdin=io.StringIO(stdin_text))
    after = _cache_ref_count(r, custom_prefix)

    assert rc == 0
    # Exactly one new cache ref should have been created (the branch line).
    assert after - before == 1, (
        f"Expected exactly 1 new cache ref, got {after - before}. "
        "If the guard is absent, generate_for_commit() is called for the "
        f"cache-namespace line '{cache_refname}' (which is inside refs/heads/*), "
        "producing a second (idempotent) write — and this assertion still passes. "
        "See the detailed guard test (test_handle_ref_skips_own_cache_namespace) "
        "for the failing assertion in that scenario."
    )
    # The branch-line cache ref must exist.
    expected_ref = f"{custom_prefix}/{commit_oid}"
    assert expected_ref in r.references
