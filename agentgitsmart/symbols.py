"""Build a symbol -> locations index for one commit using universal-ctags.

This is the artifact that kills the worst agent behavior: grep-over-the-repo,
which naively means "fetch every blob and scan it." With this index,
"where is TokenRefresher defined and who references it" is a lookup that
returns a few paths -> a few OIDs -> one batched fetch.

The indexer is allowed to read all content -- it runs *server-side* where every
blob is already local, and it does this once per commit so that agents never
have to. Content is materialized by ``git archive`` (streaming, fast) into a
temp dir; ctags emits one JSON object per tag.

If ctags is not installed, this degrades to an empty index with
``ctags_available: false`` rather than failing the push.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional

import pygit2

from . import GENERATOR_VERSION

SYMBOLS_SCHEMA = 2


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def ctags_available(ctags_bin: str = "ctags") -> bool:
    """True if ``ctags_bin`` resolves and looks like universal-ctags."""
    path = shutil.which(ctags_bin)
    if not path:
        return False
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _extract_paths(
    repo: pygit2.Repository,
    commit_oid: str,
    dest: str,
    paths: Optional[List[str]] = None,
) -> None:
    """Materialize files from ``commit_oid`` into ``dest`` via ``git archive``.

    If *paths* is None, the entire tree is extracted (full-tree mode).
    If *paths* is given, only those specific paths are extracted.
    Files land at their real relative paths so extensions are preserved.
    """
    tar_path = os.path.join(dest, "_tree.tar")
    cmd = [
        "git",
        "--git-dir",
        repo.path,
        "archive",
        "--format=tar",
        str(commit_oid),
    ]
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    with open(tar_path, "wb") as fh:
        subprocess.run(cmd, stdout=fh, check=True)
    with tarfile.open(tar_path) as tf:
        # filter='data' (py3.12+, backported to 3.11.4 / 3.10.12) refuses absolute
        # paths / traversal in members. Fall back cleanly on older interpreters --
        # the tar is produced by `git archive` of our own commit, so its members
        # are trusted, relative tree paths with no traversal.
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            tf.extractall(dest)  # pragma: no cover - older Python without filter=
    os.remove(tar_path)


# Thin alias kept for any external callers that imported the old name.
def _extract_tree(repo: pygit2.Repository, commit_oid: str, dest: str) -> None:
    """Materialize the commit's tree into ``dest`` via ``git archive``."""
    _extract_paths(repo, commit_oid, dest, paths=None)


def _parse_ctags_stream(stdout: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse the JSON-per-line ctags output into a raw symbol map.

    Returns ``{name: [{path, line, kind[, scope]}]}`` without canonicalization.
    """
    symbols: Dict[str, List[Dict[str, Any]]] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag":
            continue
        name = tag.get("name")
        path = tag.get("path", "")
        if path.startswith("./"):
            path = path[2:]
        if not name or not path:
            continue
        loc: Dict[str, Any] = {
            "path": path,
            "line": tag.get("line"),
            "kind": tag.get("kind"),
        }
        if tag.get("scope"):
            loc["scope"] = tag["scope"]
        symbols.setdefault(name, []).append(loc)
    return symbols


def _run_ctags(
    work: str,
    *,
    ctags_bin: str,
    extra_args: Optional[List[str]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Run ctags in *work* and return the raw symbol map.

    Uses ``--output-format=json --fields=+nKzS -R -f - .`` exactly.
    """
    cmd = [
        ctags_bin,
        "--output-format=json",
        "--fields=+nKzS",  # +n line, +K kind(long), +z kind key, +S signature
        "-R",
        "-f",
        "-",  # write tags to stdout
    ]
    if extra_args:
        cmd[1:1] = extra_args
    cmd.append(".")
    proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, check=True)
    return _parse_ctags_stream(proc.stdout)


# ---------------------------------------------------------------------------
# Canonicalization — the single determinism chokepoint.
# ---------------------------------------------------------------------------


def _canonical_loc(loc: Dict[str, Any]) -> Dict[str, Any]:
    """Return a location dict with a fixed key set and order."""
    out: Dict[str, Any] = {
        "path": loc["path"],
        "line": loc.get("line"),
        "kind": loc.get("kind"),
    }
    if loc.get("scope"):
        out["scope"] = loc["scope"]
    return out


def _loc_sort_key(loc: Dict[str, Any]) -> tuple:
    """Stable sort key for a location dict."""
    return (
        loc["path"],
        loc["line"] if loc["line"] is not None else -1,
        loc.get("kind") or "",
        loc.get("scope") or "",
    )


def canonicalize_symbols(
    symbols: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Return a new symbol map with names sorted and locations sorted.

    This is the single chokepoint that makes full and delta ``symbols.json``
    byte-identical (modulo ``generated_at``) when the underlying content is
    the same.
    """
    return {
        name: sorted(
            (_canonical_loc(loc) for loc in symbols[name]),
            key=_loc_sort_key,
        )
        for name in sorted(symbols)
    }


# ---------------------------------------------------------------------------
# Path inversion helper.
# ---------------------------------------------------------------------------


def invert_by_path(
    symbols: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[tuple]]:
    """Invert ``{name: [loc]}`` to ``{path: [(name, loc)]}``.

    Used by the delta builder to find which symbols live on which file so
    that only the affected paths need to be dropped from carry-forward.
    """
    result: Dict[str, List[tuple]] = {}
    for name, locs in symbols.items():
        for loc in locs:
            path = loc["path"]
            result.setdefault(path, []).append((name, loc))
    return result


# ---------------------------------------------------------------------------
# Diff classification.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffPaths:
    """Paths classified from a ``git diff --no-renames --name-status`` run.

    Attributes:
        reindex: Paths that need to be re-indexed (added / modified / type-changed).
        remove:  Paths that were deleted and must be dropped from carry-forward.
        changed_count: Total number of diff entries (one per STATUS line).
    """

    reindex: FrozenSet[str]
    remove: FrozenSet[str]
    changed_count: int


def diff_paths(
    repo: pygit2.Repository,
    parent_oid: str,
    commit_oid: str,
) -> DiffPaths:
    """Return the set of paths changed between *parent_oid* and *commit_oid*.

    Uses ``--no-renames`` so renames decompose into a deletion + addition.
    The parser still handles R/C lines defensively in case they appear.
    """
    proc = subprocess.run(
        [
            "git",
            "--git-dir",
            repo.path,
            "diff",
            "--no-renames",
            "--name-status",
            str(parent_oid),
            str(commit_oid),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    reindex: set = set()
    remove: set = set()
    entry_count = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        entry_count += 1
        status = parts[0].upper()
        if len(parts) >= 3:
            # Defensive: R/C format — STATUS<score>\told\tnew
            old_path = parts[1]
            new_path = parts[2]
            if status.startswith("R"):
                remove.add(old_path)
                reindex.add(new_path)
            elif status.startswith("C"):
                reindex.add(new_path)  # copy: new path added, original kept
        elif len(parts) == 2:
            path = parts[1]
            if status in ("A", "M", "T"):
                reindex.add(path)
            elif status == "D":
                remove.add(path)
            elif status.startswith("R"):
                # Should not appear with --no-renames but handle defensively.
                reindex.add(path)
    return DiffPaths(
        reindex=frozenset(reindex),
        remove=frozenset(remove),
        changed_count=entry_count,
    )


# ---------------------------------------------------------------------------
# Index builders.
# ---------------------------------------------------------------------------


def build_symbol_index(
    repo: pygit2.Repository,
    commit_oid: str,
    *,
    ctags_bin: str = "ctags",
    extra_args: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return a canonical symbol index dict for ``commit_oid``.

    When ctags is unavailable the returned dict has ``ctags_available: false``
    and an empty symbol map so callers can degrade gracefully.
    """
    commit = repo[commit_oid]
    if isinstance(commit, pygit2.Tag):
        commit = commit.peel(pygit2.Commit)
    source = str(commit.id)

    raw: Dict[str, List[Dict[str, Any]]] = {}
    available = ctags_available(ctags_bin)

    if available:
        with tempfile.TemporaryDirectory(prefix="agentgitsmart-ctags-") as work:
            _extract_paths(repo, source, work, paths=None)
            raw = _run_ctags(work, ctags_bin=ctags_bin, extra_args=extra_args)

    symbols = canonicalize_symbols(raw)
    return {
        "schema": SYMBOLS_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(),
        "source_commit": source,
        "ctags_available": available,
        "symbol_count": len(symbols),
        "symbols": symbols,
    }


def build_symbol_index_delta(
    repo: pygit2.Repository,
    commit_oid: str,
    parent_symbols: Dict[str, Any],
    diff: DiffPaths,
    *,
    ctags_bin: str = "ctags",
    extra_args: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a symbol index by delta-updating *parent_symbols* with *diff*.

    Carries forward all symbols whose source files were not touched, then
    re-indexes only the changed/added files.  Returns a dict shaped identically
    to :func:`build_symbol_index`.

    If ctags is unavailable at runtime (defensive), falls back to returning an
    empty canonical index with ``ctags_available: False``.
    """
    commit = repo[commit_oid]
    if isinstance(commit, pygit2.Tag):
        commit = commit.peel(pygit2.Commit)
    source = str(commit.id)

    available = ctags_available(ctags_bin)
    if not available:
        # Defensive: ctags went away between strategy decision and build.
        return {
            "schema": SYMBOLS_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "generated_at": _now_iso(),
            "source_commit": source,
            "ctags_available": False,
            "symbol_count": 0,
            "symbols": {},
        }

    # Invert parent symbol map: path -> [(name, loc)]
    by_path = invert_by_path(parent_symbols.get("symbols", {}))

    # Paths to drop entirely from carry-forward.
    drop = diff.reindex | diff.remove

    # Carry forward all (name, loc) pairs whose path was not touched.
    carried: Dict[str, List[Dict[str, Any]]] = {}
    for path, pairs in by_path.items():
        if path in drop:
            continue
        for name, loc in pairs:
            carried.setdefault(name, []).append(loc)

    # Re-index changed/added files (skip if empty — don't spawn ctags on nothing).
    new: Dict[str, List[Dict[str, Any]]] = {}
    if diff.reindex:
        with tempfile.TemporaryDirectory(prefix="agentgitsmart-delta-") as work:
            _extract_paths(repo, source, work, sorted(diff.reindex))
            new = _run_ctags(work, ctags_bin=ctags_bin, extra_args=extra_args)

    # Merge: carried paths and new paths are disjoint by construction.
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for name, locs in carried.items():
        merged.setdefault(name, []).extend(locs)
    for name, locs in new.items():
        merged.setdefault(name, []).extend(locs)

    symbols = canonicalize_symbols(merged)
    return {
        "schema": SYMBOLS_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(),
        "source_commit": source,
        "ctags_available": True,
        "symbol_count": len(symbols),
        "symbols": symbols,
    }
