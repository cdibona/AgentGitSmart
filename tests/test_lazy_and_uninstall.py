"""Tests for lazy cache generation (service) and the erase/uninstall tool."""

from __future__ import annotations

import pygit2
import pytest

from agentgitsmart.service import create_app
from agentgitsmart.uninstall import erase, find_cache_refs


# ── Lazy generation ──────────────────────────────────────────────────────


@pytest.fixture
def client(repo, cfg):
    """A test client over a repo that has NO cache yet (lazy gen will build it)."""
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app.test_client(), repo[1]


def test_manifest_lazily_generated_on_first_request(client, repo, cfg):
    """First /manifest request for an uncached commit builds the cache."""
    c, commit = client
    r, _ = repo
    ref = f"{cfg.ref_prefix}/{commit}"
    assert ref not in r.references  # no cache exists yet

    resp = c.get(f"/cache/{commit}/manifest")
    assert resp.status_code == 200
    assert resp.get_json()["source_commit"] == commit

    # The side ref now exists — it was built on demand.
    r2 = pygit2.Repository(cfg.repo_dir)
    assert ref in r2.references


def test_resolve_lazily_generated(client):
    c, commit = client
    resp = c.post(
        f"/cache/{commit}/resolve",
        json={"paths": ["src/app.py"]},
    )
    assert resp.status_code == 200
    assert resp.get_json()["fetch_oids"]


def test_lazy_disabled_returns_404(repo):
    """With lazy_generation off, an uncached commit 404s instead of building."""
    from agentgitsmart.config import AgentGitSmartConfig

    r, commit = repo
    cfg = AgentGitSmartConfig(repo_dir=r.path, lazy_generation=False)
    app = create_app(cfg)
    app.config.update(TESTING=True)
    c = app.test_client()
    assert c.get(f"/cache/{commit}/manifest").status_code == 404


def test_lazy_unknown_commit_404(client):
    """A commit that doesn't exist in the repo can't be generated → 404."""
    c, _ = client
    assert c.get("/cache/%s/manifest" % ("0" * 40)).status_code == 404


def test_manifest_handles_gitlink_submodule(repo, cfg):
    """A gitlink (submodule) entry must not crash manifest building.

    git.git and many real repos carry submodule gitlinks whose commit object
    lives in another repo and is absent locally; build_manifest must skip the
    object lookup for mode 160000 instead of raising KeyError.
    """
    import pygit2

    from agentgitsmart.manifest import build_manifest

    r, _ = repo
    # Craft a tree containing a gitlink pointing at an arbitrary (absent) commit.
    # Use a valid-looking non-null OID — git accepts any SHA for a gitlink and
    # does NOT require it to exist locally (that's the whole point of the bug).
    absent_commit = "deadbeef" * 5  # 40 hex chars, not in this repo
    tb = r.TreeBuilder()
    tb.insert("submod", absent_commit, pygit2.GIT_FILEMODE_COMMIT)
    tb.insert("real.txt", r.create_blob(b"hi\n"), pygit2.GIT_FILEMODE_BLOB)
    tree_oid = tb.write()
    sig = pygit2.Signature("t", "t@t")
    commit_oid = r.create_commit(None, sig, sig, "with submodule", tree_oid, [])

    man = build_manifest(r, str(commit_oid))
    by_path = {e["path"]: e for e in man["entries"]}
    assert by_path["submod"]["mode"] == "160000"
    assert by_path["submod"]["size"] is None  # gitlink: never dereferenced
    assert by_path["real.txt"]["size"] == 3


# ── Erase / uninstall ────────────────────────────────────────────────────


def test_erase_dry_run_changes_nothing(repo, cfg):
    from agentgitsmart.hook import generate_for_commit

    r, commit = repo
    generate_for_commit(r, commit, cfg)
    assert len(find_cache_refs(r)) == 1

    summary = erase(r.path, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["cache_ref_count"] == 1
    # Ref still present after a dry run.
    assert len(find_cache_refs(pygit2.Repository(r.path))) == 1


def test_erase_removes_all_cache_refs(repo, cfg):
    from agentgitsmart.hook import generate_for_commit

    r, commit = repo
    generate_for_commit(r, commit, cfg)
    assert len(find_cache_refs(r)) == 1

    summary = erase(r.path, dry_run=False, gc=False)
    assert summary["dry_run"] is False
    assert summary["cache_ref_count"] == 1

    # Reopen: no agent-git-smart refs remain.
    r2 = pygit2.Repository(r.path)
    assert find_cache_refs(r2) == []


def test_erase_leaves_normal_history_untouched(repo, cfg):
    from agentgitsmart.hook import generate_for_commit

    r, commit = repo
    generate_for_commit(r, commit, cfg)
    head_before = str(r.references["refs/heads/master"].target)

    erase(r.path, dry_run=False)

    r2 = pygit2.Repository(r.path)
    head_after = str(r2.references["refs/heads/master"].target)
    assert head_after == head_before  # branch tip unchanged
