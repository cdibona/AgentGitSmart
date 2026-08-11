"""Symbol-index tests. Skip the ctags-dependent assertions when ctags is
absent, but still verify graceful degradation."""

from __future__ import annotations

import pytest

from agentgitsmart import symbols as sym


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
    assert any(loc["path"] == "src/app.py" for loc in locs)


@pytest.mark.skipif(not sym.ctags_available(), reason="universal-ctags not installed")
def test_full_index_is_canonically_sorted(repo):
    """Symbol names must be sorted; each symbol's locations must be sorted."""
    r, commit = repo
    idx = sym.build_symbol_index(r, commit)
    symbols = idx["symbols"]

    # Symbol names must be in ascending lexicographic order.
    assert list(symbols.keys()) == sorted(symbols.keys()), "Symbol names are not sorted"

    # Each symbol's location list must be sorted by the canonical key.
    for name, locs in symbols.items():
        sort_keys = [sym._loc_sort_key(loc) for loc in locs]
        assert sort_keys == sorted(sort_keys), (
            f"Locations for symbol {name!r} are not sorted"
        )
