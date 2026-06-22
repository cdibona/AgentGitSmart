"""Symbol-index tests. Skip the ctags-dependent assertions when ctags is
absent, but still verify graceful degradation."""
from __future__ import annotations

import pytest

from agentcache import symbols as sym


def test_degrades_gracefully_without_ctags(repo):
    r, commit = repo
    # Point at a binary that does not exist -> ctags_available False, no crash.
    idx = sym.build_symbol_index(r, commit, ctags_bin="definitely-not-ctags-xyz")
    assert idx["ctags_available"] is False
    assert idx["symbols"] == {}
    assert idx["symbol_count"] == 0
    assert idx["source_commit"] == commit


@pytest.mark.skipif(not sym.ctags_available(), reason="universal-ctags not installed")
def test_indexes_known_symbols(repo):
    r, commit = repo
    idx = sym.build_symbol_index(r, commit)
    assert idx["ctags_available"] is True
    # Symbols defined in the fixture files.
    assert "TokenRefresher" in idx["symbols"]
    assert "make_refresher" in idx["symbols"]
    assert "str_len" in idx["symbols"]
    # Location points at the right file (path normalized, no leading ./).
    locs = idx["symbols"]["TokenRefresher"]
    assert any(l["path"] == "src/app.py" for l in locs)
