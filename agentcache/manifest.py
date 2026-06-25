"""Build the flat path -> (oid, size, mode) manifest for one commit.

This is the cheapest, highest-value artifact: with a blobless clone the agent
already holds every tree, so it *can* resolve paths itself, but the manifest
(a) carries size so the agent never fetches a 2 GB asset by accident, (b) is a
single sequential read instead of thousands of tree lookups, and (c) lets you
drop to ``--filter=tree:0`` because read-planning no longer needs tree objects.

The flat listing is produced with ``Index.read_tree``, which expands a tree
into its full recursive set of entries with complete paths -- no hand-rolled
recursion required.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict

import pygit2

from . import GENERATOR_VERSION

MANIFEST_SCHEMA = 1


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def build_manifest(repo: pygit2.Repository, commit_oid: str) -> Dict[str, Any]:
    """Return a manifest dict for ``commit_oid`` (a hex string or Oid)."""
    commit = repo[commit_oid]
    if isinstance(commit, pygit2.Tag):  # be forgiving about annotated tags
        commit = commit.peel(pygit2.Commit)
    tree = commit.tree

    index = pygit2.Index()
    index.read_tree(tree)

    # gitlink / submodule entries (mode 160000) point at a commit that lives in
    # *another* repository, so it is NOT in this object store.  Looking it up
    # would raise KeyError and abort the whole manifest (real repos like git.git
    # use submodules), so detect them by mode and never dereference them.
    GITLINK_MODE = 0o160000

    entries = []
    for entry in index:
        size = None
        if entry.mode != GITLINK_MODE:
            try:
                obj = repo[entry.id]
                size = obj.size if isinstance(obj, pygit2.Blob) else None
            except KeyError:
                # Object genuinely absent locally — be robust, leave size unknown
                # rather than failing the entire manifest build.
                size = None
        entries.append(
            {
                "path": entry.path,
                "oid": str(entry.id),
                "mode": format(entry.mode, "06o"),
                "size": size,
            }
        )
    entries.sort(key=lambda e: e["path"])

    return {
        "schema": MANIFEST_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(),
        "source_commit": str(commit.id),
        "tree": str(tree.id),
        "entry_count": len(entries),
        "entries": entries,
    }
