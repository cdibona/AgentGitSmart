"""Service tests: the endpoints an agent VM hits to plan its batched fetch."""

from __future__ import annotations

import json

import pytest

from agentgitsmart.hook import generate_for_commit
from agentgitsmart.service import create_app


@pytest.fixture
def client(repo, cfg):
    r, commit = repo
    generate_for_commit(r, commit, cfg)  # populate the side ref
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app.test_client(), commit


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").get_json()["status"] == "ok"


def test_caches_lists_commit(client):
    c, commit = client
    assert commit in c.get("/caches").get_json()["commits"]


def test_manifest_endpoint(client):
    c, commit = client
    resp = c.get(f"/cache/{commit}/manifest")
    assert resp.status_code == 200
    man = resp.get_json()
    assert man["source_commit"] == commit
    assert any(e["path"] == "src/app.py" for e in man["entries"])


def test_resolve_returns_fetch_oids(client):
    c, commit = client
    resp = c.post(
        f"/cache/{commit}/resolve",
        data=json.dumps({"paths": ["src/app.py", "ghost.py"]}),
        content_type="application/json",
    )
    body = resp.get_json()
    assert [r["path"] for r in body["resolved"]] == ["src/app.py"]
    assert body["missing"] == ["ghost.py"]
    # The agent feeds fetch_oids straight into `git fetch origin <oids>`.
    assert len(body["fetch_oids"]) == 1
    assert len(body["fetch_oids"][0]) == 40
    assert body["total_bytes"] > 0


def test_manifest_404_for_unknown_commit(client):
    c, _ = client
    assert c.get("/cache/%s/manifest" % ("0" * 40)).status_code == 404
