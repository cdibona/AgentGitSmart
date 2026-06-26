"""Tests for graceful cache-taint detection and fallback.

Covers three behaviours:
  (1) Serve-time version/schema re-check: stale manifest/symbols → regenerate.
  (2) Targeted on-miss taint probe in /resolve: missing path in real tree → rebuild.
  (3) Agent-side graceful fallback: empty agentcache manifest → blobless ls-tree.

Run with: .venv/bin/pytest tests/test_taint_fallback.py -v
"""

from __future__ import annotations

import json

import pygit2
import pytest

from agentcache import GENERATOR_VERSION
from agentcache import cache_writer
from agentcache.hook import generate_for_commit
from agentcache.service import create_app
from tests.conftest import make_commit


# ---------------------------------------------------------------------------
# (a) test_out_of_band_commit_lazily_built
# ---------------------------------------------------------------------------


def test_out_of_band_commit_lazily_built(repo, cfg):
    """Human commits without hook firing → lazy cache built on first GET /manifest."""
    r, c1 = repo

    # Create a second commit manually (no hook, no write_cache call).
    c2 = make_commit(r, c1, files={"docs/readme.md": "# docs\n"})

    ref_c2 = f"{cfg.ref_prefix}/{c2}"
    assert ref_c2 not in r.references, "C2 must not have a cache yet"

    app = create_app(cfg)
    app.config.update(TESTING=True)
    client = app.test_client()

    resp = client.get(f"/cache/{c2}/manifest")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    data = resp.get_json()
    assert data["source_commit"] == c2
    assert any(e["path"] == "docs/readme.md" for e in data["entries"])

    # The side ref must now exist (lazy generation fired).
    r2 = pygit2.Repository(cfg.repo_dir)
    assert ref_c2 in r2.references, "Cache ref must exist after lazy build"


# ---------------------------------------------------------------------------
# (b) test_resolve_taint_triggers_rebuild
# ---------------------------------------------------------------------------


def test_resolve_taint_triggers_rebuild(repo, cfg):
    """Stale manifest missing a real file → /resolve detects taint and rebuilds."""
    r, c1 = repo

    # C1: generate correct cache.
    generate_for_commit(r, c1, cfg)

    # C2: add a new file, then generate correct cache for it.
    c2 = make_commit(r, c1, files={"src/new_module.py": "# new module\n"})
    generate_for_commit(r, c2, cfg)

    # Read C2's correct artifacts.
    man_raw = cache_writer.read_artifact(
        r, c2, "manifest.json", ref_prefix=cfg.ref_prefix
    )
    sym_raw = cache_writer.read_artifact(
        r, c2, "symbols.json", ref_prefix=cfg.ref_prefix
    )
    meta_raw = cache_writer.read_artifact(
        r, c2, "meta.json", ref_prefix=cfg.ref_prefix
    )
    agents_raw = cache_writer.read_artifact(
        r, c2, "agents.md", ref_prefix=cfg.ref_prefix
    )

    # Deliberately strip "src/new_module.py" from the manifest — simulates a
    # stale/corrupt cache that doesn't know about the new file.
    man = json.loads(man_raw)
    man["entries"] = [e for e in man["entries"] if e["path"] != "src/new_module.py"]
    man["entry_count"] = len(man["entries"])
    stale_man_raw = json.dumps(man, separators=(",", ":")).encode()

    # Overwrite C2's cache ref with the stale manifest.
    cache_writer.write_cache(
        r,
        c2,
        {
            "manifest.json": stale_man_raw,
            "symbols.json": sym_raw,
            "meta.json": meta_raw,
            "agents.md": agents_raw,
        },
        ref_prefix=cfg.ref_prefix,
    )

    # Confirm the stale manifest is in place.
    stale_check = json.loads(
        cache_writer.read_artifact(r, c2, "manifest.json", ref_prefix=cfg.ref_prefix)
    )
    assert not any(e["path"] == "src/new_module.py" for e in stale_check["entries"])

    # POST /resolve with the path that's missing from the stale manifest but
    # present in C2's actual git tree.
    app = create_app(cfg)
    app.config.update(TESTING=True)
    client = app.test_client()

    resp = client.post(
        f"/cache/{c2}/resolve",
        json={"paths": ["src/new_module.py"]},
    )
    assert resp.status_code == 200
    body = resp.get_json()

    # The manifest was rebuilt → rebuilt=True.
    assert body["rebuilt"] is True, f"Expected rebuilt=True, got: {body}"

    # The file is now resolved, not missing.
    resolved_paths = [e["path"] for e in body["resolved"]]
    assert "src/new_module.py" in resolved_paths, (
        f"Expected src/new_module.py in resolved, got: {body}"
    )
    assert "src/new_module.py" not in body["missing"]

    # OID must be a valid-looking 40-char hex.
    resolved_entry = next(e for e in body["resolved"] if e["path"] == "src/new_module.py")
    assert len(resolved_entry["oid"]) == 40


# ---------------------------------------------------------------------------
# (c) test_resolve_genuinely_absent_no_rebuild
# ---------------------------------------------------------------------------


def test_resolve_genuinely_absent_no_rebuild(repo, cfg):
    """Path absent from BOTH manifest AND real tree → no rebuild, stays missing."""
    r, c1 = repo
    generate_for_commit(r, c1, cfg)

    ref = f"{cfg.ref_prefix}/{c1}"
    cache_oid_before = str(r.references[ref].target)

    app = create_app(cfg)
    app.config.update(TESTING=True)
    client = app.test_client()

    resp = client.post(
        f"/cache/{c1}/resolve",
        json={"paths": ["nonexistent/totally_missing.py"]},
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["rebuilt"] is False, f"Expected rebuilt=False, got: {body}"
    assert "nonexistent/totally_missing.py" in body["missing"]
    assert not any(
        e["path"] == "nonexistent/totally_missing.py" for e in body["resolved"]
    )

    # Cache ref must not have moved — no needless regeneration.
    r2 = pygit2.Repository(cfg.repo_dir)
    cache_oid_after = str(r2.references[ref].target)
    assert cache_oid_after == cache_oid_before, (
        "Cache ref must be unchanged for a genuinely absent path"
    )


# ---------------------------------------------------------------------------
# (d) test_stale_version_regenerated_on_serve
# ---------------------------------------------------------------------------


def test_stale_version_regenerated_on_serve(repo, cfg):
    """Stale generator_version in manifest → regenerated before serving."""
    r, c1 = repo
    generate_for_commit(r, c1, cfg)

    # Read all four artifacts so we can re-write them with a stale manifest.
    man_raw = cache_writer.read_artifact(
        r, c1, "manifest.json", ref_prefix=cfg.ref_prefix
    )
    sym_raw = cache_writer.read_artifact(
        r, c1, "symbols.json", ref_prefix=cfg.ref_prefix
    )
    meta_raw = cache_writer.read_artifact(
        r, c1, "meta.json", ref_prefix=cfg.ref_prefix
    )
    agents_raw = cache_writer.read_artifact(
        r, c1, "agents.md", ref_prefix=cfg.ref_prefix
    )

    # Overwrite manifest generator_version with a stale value.
    man = json.loads(man_raw)
    man["generator_version"] = "0.0.0-old"
    stale_man_raw = json.dumps(man, separators=(",", ":")).encode()

    cache_writer.write_cache(
        r,
        c1,
        {
            "manifest.json": stale_man_raw,
            "symbols.json": sym_raw,
            "meta.json": meta_raw,
            "agents.md": agents_raw,
        },
        ref_prefix=cfg.ref_prefix,
    )

    # Confirm the stale manifest is in place.
    stale_check = json.loads(
        cache_writer.read_artifact(r, c1, "manifest.json", ref_prefix=cfg.ref_prefix)
    )
    assert stale_check["generator_version"] == "0.0.0-old"

    # Create the Flask app and hit /manifest — staleness check must regenerate.
    app = create_app(cfg)
    app.config.update(TESTING=True)
    client = app.test_client()

    resp = client.get(f"/cache/{c1}/manifest")
    assert resp.status_code == 200

    served = resp.get_json()
    assert served["generator_version"] == GENERATOR_VERSION, (
        f"Expected GENERATOR_VERSION={GENERATOR_VERSION!r}, "
        f"got {served['generator_version']!r} — staleness check did not regenerate"
    )

    # The ref must still point at a (newly written) commit.
    r2 = pygit2.Repository(cfg.repo_dir)
    assert f"{cfg.ref_prefix}/{c1}" in r2.references


# ---------------------------------------------------------------------------
# (e) test_no_thrash_when_fresh
# ---------------------------------------------------------------------------


def test_no_thrash_when_fresh(repo, cfg):
    """Fresh cache + all-present paths → rebuilt=False; ref unchanged across requests."""
    r, c1 = repo
    generate_for_commit(r, c1, cfg)

    ref = f"{cfg.ref_prefix}/{c1}"
    cache_oid_before = str(r.references[ref].target)

    app = create_app(cfg)
    app.config.update(TESTING=True)
    client = app.test_client()

    resp1 = client.post(f"/cache/{c1}/resolve", json={"paths": ["src/app.py"]})
    resp2 = client.post(f"/cache/{c1}/resolve", json={"paths": ["src/app.py"]})

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    body1 = resp1.get_json()
    body2 = resp2.get_json()
    assert body1["rebuilt"] is False, f"First request must not rebuild: {body1}"
    assert body2["rebuilt"] is False, f"Second request must not rebuild: {body2}"
    assert body1["resolved"][0]["path"] == "src/app.py"

    # Cache ref OID must not have changed (no unnecessary regeneration).
    r2 = pygit2.Repository(cfg.repo_dir)
    cache_oid_after = str(r2.references[ref].target)
    assert cache_oid_after == cache_oid_before, (
        "Cache ref must not move for a fresh cache with present paths"
    )


# ---------------------------------------------------------------------------
# (f) test_agent_fallback_on_empty_manifest
# ---------------------------------------------------------------------------


def test_agent_fallback_on_empty_manifest():
    """Pure-function helper: empty agentcache manifest → blobless fallback, no crash."""
    from testharness.real_agent import _apply_empty_manifest_fallback

    blobless_files = ["src/main.py", "lib/util.rs", "README.md"]

    # --- Case 1: empty agentcache list → use fallback ---
    metrics: dict = {"agentcache_detected": True}
    result = _apply_empty_manifest_fallback(
        agentcache_files=[],
        fallback_files=blobless_files,
        metrics=metrics,
    )
    assert result == blobless_files, (
        "Should return blobless list when agentcache is empty"
    )
    assert metrics.get("fallback") == "blobless (empty or tainted manifest)", (
        f"Expected fallback key in metrics, got: {metrics}"
    )
    assert metrics["agentcache_detected"] is False, (
        "agentcache_detected must be cleared on fallback"
    )

    # --- Case 2: non-empty agentcache list → return it unchanged, no fallback ---
    metrics2: dict = {"agentcache_detected": True}
    result2 = _apply_empty_manifest_fallback(
        agentcache_files=["src/main.py"],
        fallback_files=blobless_files,
        metrics=metrics2,
    )
    assert result2 == ["src/main.py"], (
        "Should return agentcache list unchanged when non-empty"
    )
    assert "fallback" not in metrics2, (
        f"No fallback key expected for non-empty agentcache: {metrics2}"
    )
    assert metrics2["agentcache_detected"] is True
