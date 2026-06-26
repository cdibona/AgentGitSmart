"""Delta symbol indexing + load observability tests.

Tests 1-10 are skipped when universal-ctags is absent; test 11 never skips
because it exercises the ctags-absent code path explicitly.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pygit2
import pytest

from agentcache import cache_writer
from agentcache import hook
from agentcache import symbols as sym
from tests.conftest import FILES, make_commit

# ---------------------------------------------------------------------------
# Skip decorator shared by tests 1-10.
# ---------------------------------------------------------------------------

_SKIP_NO_CTAGS = pytest.mark.skipif(
    not sym.ctags_available(), reason="universal-ctags not installed"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask(raw: bytes) -> str:
    """Remove volatile ``generated_at`` from a symbols.json blob for comparison."""
    d = json.loads(raw)
    d.pop("generated_at", None)
    return json.dumps(d, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Test 1: first generation is always a full build.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_first_generation_is_full(repo, cfg):
    """Root commit (no parents) must produce a full build with no fallback reason."""
    r, commit = repo
    result = hook.generate_for_commit(r, commit, cfg)
    gen = result["generation"]

    assert gen["mode"] == "full"
    assert gen["parent"] is None
    assert gen["fallback_reason"] is None
    assert gen["files_reindexed"] == gen["files_in_tree"]
    assert gen["files_carried_forward"] == 0


# ---------------------------------------------------------------------------
# Test 2: single changed file triggers a targeted delta.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_single_file_change_is_delta(repo, cfg):
    """Changing one file must produce a delta; all untouched symbols survive."""
    r, base_commit = repo
    # Cache the base commit so the child can delta against it.
    hook.generate_for_commit(r, base_commit, cfg)

    new_content = "# new\ndef helper():\n    pass\n"
    child = make_commit(r, base_commit, {"src/app.py": new_content})
    result = hook.generate_for_commit(r, child, cfg)
    gen = result["generation"]

    assert gen["mode"] == "delta"
    assert gen["parent"] == base_commit
    assert gen["files_changed"] == 1
    assert gen["files_reindexed"] == 1
    assert gen["files_carried_forward"] == gen["files_in_tree"] - 1
    assert gen["content_bytes_materialized"] == len(new_content.encode())

    # A symbol from the untouched src/util.c must still be present.
    raw = cache_writer.read_artifact(
        r, child, "symbols.json", ref_prefix=cfg.ref_prefix
    )
    syms = json.loads(raw)
    assert "str_len" in syms["symbols"], "Carry-forward failed: str_len missing"


# ---------------------------------------------------------------------------
# Test 3: delta and full produce byte-identical symbols (modulo generated_at).
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_delta_matches_full_byte_identical(repo, cfg):
    """Delta-built symbols.json must be byte-identical to full-built, sans generated_at."""
    r, base_commit = repo
    hook.generate_for_commit(r, base_commit, cfg)

    child = make_commit(r, base_commit, {"extra.py": "def foo():\n    pass\n"})

    # Delta build.
    result_delta = hook.generate_for_commit(r, child, cfg)
    assert result_delta["generation"]["mode"] == "delta", (
        "Expected delta on first child build"
    )
    delta_bytes = cache_writer.read_artifact(
        r, child, "symbols.json", ref_prefix=cfg.ref_prefix
    )

    # Delete the parent cache ref to force a full rebuild of the child.
    base_ref = f"{cfg.ref_prefix}/{base_commit}"
    r.references[base_ref].delete()

    # Full build.
    result_full = hook.generate_for_commit(r, child, cfg)
    assert result_full["generation"]["mode"] == "full", (
        "Expected full after parent ref deleted"
    )
    full_bytes = cache_writer.read_artifact(
        r, child, "symbols.json", ref_prefix=cfg.ref_prefix
    )

    assert _mask(delta_bytes) == _mask(full_bytes), (
        "Delta and full symbol indexes differ (masked generated_at):\n"
        f"  delta: {_mask(delta_bytes)[:200]}\n"
        f"  full:  {_mask(full_bytes)[:200]}"
    )
    assert (
        json.loads(delta_bytes)["symbol_count"]
        == json.loads(full_bytes)["symbol_count"]
    )


# ---------------------------------------------------------------------------
# Test 4: uncached parent forces full build.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_fallback_no_parent_cache_is_full(repo, cfg):
    """Child whose parent has no cache ref must fall back to a full build."""
    r, base_commit = repo
    # Intentionally do NOT cache base_commit.
    child = make_commit(r, base_commit, {"extra.py": "x = 1\n"})
    result = hook.generate_for_commit(r, child, cfg)
    gen = result["generation"]

    assert gen["mode"] == "full"
    assert gen["fallback_reason"] == "parent_uncached"


# ---------------------------------------------------------------------------
# Test 5: generator version mismatch forces full build.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_fallback_version_mismatch_is_full(repo, cfg):
    """Parent symbols with a stale generator_version must trigger a full rebuild."""
    r, base_commit = repo
    # Build and cache the base commit.
    hook.generate_for_commit(r, base_commit, cfg)

    # Read, corrupt, and re-write the base's symbols.json.
    raw_syms = cache_writer.read_artifact(
        r, base_commit, "symbols.json", ref_prefix=cfg.ref_prefix
    )
    syms_data = json.loads(raw_syms)
    syms_data["generator_version"] = "0.0.0-old"

    artifacts = {
        "manifest.json": cache_writer.read_artifact(
            r, base_commit, "manifest.json", ref_prefix=cfg.ref_prefix
        ),
        "symbols.json": json.dumps(syms_data, separators=(",", ":")).encode(),
        "meta.json": cache_writer.read_artifact(
            r, base_commit, "meta.json", ref_prefix=cfg.ref_prefix
        ),
        "agents.md": cache_writer.read_artifact(
            r, base_commit, "agents.md", ref_prefix=cfg.ref_prefix
        ),
    }
    cache_writer.write_cache(
        r,
        base_commit,
        artifacts,
        ref_prefix=cfg.ref_prefix,
        bot_name=cfg.bot_name,
        bot_email=cfg.bot_email,
    )

    child = make_commit(r, base_commit, {"extra.py": "x = 1\n"})
    result = hook.generate_for_commit(r, child, cfg)
    gen = result["generation"]

    assert gen["mode"] == "full"
    assert gen["fallback_reason"] == "version_mismatch"


# ---------------------------------------------------------------------------
# Test 6: deleted file's symbols disappear from the index.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_delete_handled(repo, cfg):
    """Removing a file must purge its symbols; metrics must reflect pure deletion."""
    r, base_commit = repo
    hook.generate_for_commit(r, base_commit, cfg)

    # Remove src/util.c which defines str_len.
    child = make_commit(r, base_commit, removed=["src/util.c"])
    result = hook.generate_for_commit(r, child, cfg)
    gen = result["generation"]

    assert gen["mode"] == "delta"
    assert gen["files_changed"] == 1
    assert gen["files_reindexed"] == 0
    assert gen["content_bytes_materialized"] == 0

    raw = cache_writer.read_artifact(
        r, child, "symbols.json", ref_prefix=cfg.ref_prefix
    )
    syms = json.loads(raw)
    assert "str_len" not in syms["symbols"], (
        "str_len should be gone after deleting src/util.c"
    )
    # str_len must not appear in any symbol's locations.
    for locs in syms["symbols"].values():
        for loc in locs:
            assert loc["path"] != "src/util.c", "src/util.c still appears in a location"


# ---------------------------------------------------------------------------
# Test 7: rename (delete + add) reindexes the new path, drops the old one.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_rename_handled(repo, cfg):
    """Renaming a file (delete old + add new) must reindex the new path only."""
    r, base_commit = repo
    hook.generate_for_commit(r, base_commit, cfg)

    old_path = "src/util.c"
    new_path = "src/util_renamed.c"
    # Use the same content so str_len survives the rename.
    child = make_commit(
        r,
        base_commit,
        {new_path: FILES[old_path]},
        removed=[old_path],
    )
    result = hook.generate_for_commit(r, child, cfg)
    gen = result["generation"]

    assert gen["mode"] == "delta"
    # Both D and A entries appear -> changed_count==2, reindexed==1.
    assert gen["files_reindexed"] == 1

    raw = cache_writer.read_artifact(
        r, child, "symbols.json", ref_prefix=cfg.ref_prefix
    )
    syms = json.loads(raw)
    # str_len must be at the new path.
    if "str_len" in syms["symbols"]:
        locs = syms["symbols"]["str_len"]
        assert any(loc["path"] == new_path for loc in locs), (
            f"str_len not found at {new_path}"
        )
        assert not any(loc["path"] == old_path for loc in locs), (
            f"str_len still at old {old_path}"
        )


# ---------------------------------------------------------------------------
# Test 8: ratio threshold overrides delta and forces a full rebuild.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_ratio_threshold_forces_full(repo, cfg):
    """Exceeding delta_max_ratio must downgrade to a full build."""
    r, base_commit = repo
    # FILES has 3 files; changing 1 gives ratio ≈ 0.33, well above 0.01.
    cfg2 = replace(cfg, delta_max_ratio=0.01)
    hook.generate_for_commit(r, base_commit, cfg2)

    child = make_commit(r, base_commit, {"src/app.py": "x = 1\n"})
    result = hook.generate_for_commit(r, child, cfg2)
    gen = result["generation"]

    assert gen["mode"] == "full"
    assert gen["fallback_reason"] == "ratio_threshold"


# ---------------------------------------------------------------------------
# Test 9a: merge commits default to delta against first parent.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_merge_commit_is_delta_by_default(repo, cfg):
    """Merge commit with delta_on_merge=True (default) must delta against P1."""
    r, base_commit = repo

    # P1 adds branch1.py (master branch, advancing from base).
    p1 = make_commit(r, base_commit, {"branch1.py": "x = 1\n"})
    # P2 adds branch2.py (separate branch from base).
    p2 = make_commit(
        r, base_commit, {"branch2.py": "y = 2\n"}, branch="refs/heads/branch2"
    )

    # Cache P1 (and NOT base — so base is parent_uncached for P1, hence full for P1).
    hook.generate_for_commit(r, p1, cfg)

    # Create merge commit M with parents [P1, P2] and tree = P1's tree.
    p1_commit = r[p1]
    if isinstance(p1_commit, pygit2.Tag):
        p1_commit = p1_commit.peel(pygit2.Commit)
    p2_commit = r[p2]
    if isinstance(p2_commit, pygit2.Tag):
        p2_commit = p2_commit.peel(pygit2.Commit)

    sig = pygit2.Signature("Test Author", "test@example.com")
    # Don't update any ref — just create the orphan-like commit object.
    merge_oid = r.create_commit(
        None,  # no ref update
        sig,
        sig,
        "Merge branches\n",
        p1_commit.tree.id,
        [p1_commit.id, p2_commit.id],
    )
    merge_hex = str(merge_oid)

    result = hook.generate_for_commit(r, merge_hex, cfg)
    gen = result["generation"]

    assert gen["mode"] == "delta"
    assert gen["parent"] == p1


# ---------------------------------------------------------------------------
# Test 9b: merge commits fall back to full when delta_on_merge is disabled.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_merge_full_when_disabled(repo, cfg):
    """Merge commit with delta_on_merge=False must fall back to full build."""
    r, base_commit = repo
    cfg2 = replace(cfg, delta_on_merge=False)

    p1 = make_commit(r, base_commit, {"branch1.py": "x = 1\n"})
    p2 = make_commit(
        r, base_commit, {"branch2.py": "y = 2\n"}, branch="refs/heads/branch2"
    )
    hook.generate_for_commit(r, p1, cfg2)

    p1_commit = r[p1]
    if isinstance(p1_commit, pygit2.Tag):
        p1_commit = p1_commit.peel(pygit2.Commit)
    p2_commit = r[p2]
    if isinstance(p2_commit, pygit2.Tag):
        p2_commit = p2_commit.peel(pygit2.Commit)

    sig = pygit2.Signature("Test Author", "test@example.com")
    merge_oid = r.create_commit(
        None,
        sig,
        sig,
        "Merge branches\n",
        p1_commit.tree.id,
        [p1_commit.id, p2_commit.id],
    )
    merge_hex = str(merge_oid)

    result = hook.generate_for_commit(r, merge_hex, cfg2)
    gen = result["generation"]

    assert gen["mode"] == "full"
    assert gen["fallback_reason"] == "merge_commit"


# ---------------------------------------------------------------------------
# Test 10: generation block has the correct shape and types.
# ---------------------------------------------------------------------------


@_SKIP_NO_CTAGS
def test_generation_block_shape(repo, cfg):
    """Every field in the generation schema must be present with the right type."""
    r, commit = repo
    result = hook.generate_for_commit(r, commit, cfg)
    gen = result["generation"]

    expected_keys = {
        "mode",
        "parent",
        "files_in_tree",
        "files_changed",
        "files_reindexed",
        "files_carried_forward",
        "content_bytes_materialized",
        "symbol_count",
        "ctags_available",
        "fallback_reason",
    }
    assert set(gen.keys()) == expected_keys

    assert isinstance(gen["mode"], str)
    assert gen["parent"] is None or isinstance(gen["parent"], str)
    assert isinstance(gen["files_in_tree"], int)
    assert isinstance(gen["files_changed"], int)
    assert isinstance(gen["files_reindexed"], int)
    assert isinstance(gen["files_carried_forward"], int)
    assert isinstance(gen["content_bytes_materialized"], int)
    assert isinstance(gen["symbol_count"], int)
    assert isinstance(gen["ctags_available"], bool)
    assert gen["fallback_reason"] is None or isinstance(gen["fallback_reason"], str)

    # Also verify that meta.json written to disk has the generation block.
    raw_meta = cache_writer.read_artifact(
        r, commit, "meta.json", ref_prefix=cfg.ref_prefix
    )
    meta = json.loads(raw_meta)
    assert "generation" in meta
    assert meta["generation"] == gen


# ---------------------------------------------------------------------------
# Test 11: NO SKIP — exercises the ctags-absent fallback path.
# ---------------------------------------------------------------------------


def test_degrades_to_full_without_ctags(repo, cfg):
    """With ctags unavailable the strategy must degrade to a full build gracefully."""
    r, base_commit = repo
    cfg3 = replace(cfg, ctags_bin="definitely-not-ctags")

    # Cache base with the fake ctags binary (produces empty symbols, ctags_available=False).
    hook.generate_for_commit(r, base_commit, cfg3)

    child = make_commit(r, base_commit, {"extra.py": "x = 1\n"})
    result = hook.generate_for_commit(r, child, cfg3)
    gen = result["generation"]

    assert gen["mode"] == "full"
    assert gen["fallback_reason"] == "ctags_unavailable"
    assert gen["ctags_available"] is False
    assert gen["files_reindexed"] == 0
    assert gen["content_bytes_materialized"] == 0
