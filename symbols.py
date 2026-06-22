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
from typing import Any, Dict, List, Optional

import pygit2

from . import GENERATOR_VERSION

SYMBOLS_SCHEMA = 1


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


def _extract_tree(repo: pygit2.Repository, commit_oid: str, dest: str) -> None:
    """Materialize the commit's tree into ``dest`` via ``git archive``."""
    tar_path = os.path.join(dest, "_tree.tar")
    with open(tar_path, "wb") as fh:
        subprocess.run(
            ["git", "--git-dir", repo.path, "archive", "--format=tar", str(commit_oid)],
            stdout=fh,
            check=True,
        )
    with tarfile.open(tar_path) as tf:
        # filter='data' (py3.12+) refuses absolute paths / traversal in members.
        tf.extractall(dest, filter="data")
    os.remove(tar_path)


def build_symbol_index(
    repo: pygit2.Repository,
    commit_oid: str,
    *,
    ctags_bin: str = "ctags",
    extra_args: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return ``{name: [{path, line, kind, scope}]}`` for ``commit_oid``."""
    commit = repo[commit_oid]
    if isinstance(commit, pygit2.Tag):
        commit = commit.peel(pygit2.Commit)
    source = str(commit.id)

    symbols: Dict[str, List[Dict[str, Any]]] = {}
    available = ctags_available(ctags_bin)

    if available:
        with tempfile.TemporaryDirectory(prefix="agentcache-ctags-") as work:
            _extract_tree(repo, source, work)
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
            proc = subprocess.run(
                cmd, cwd=work, capture_output=True, text=True, check=True
            )
            for line in proc.stdout.splitlines():
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
                loc = {"path": path, "line": tag.get("line"), "kind": tag.get("kind")}
                if tag.get("scope"):
                    loc["scope"] = tag["scope"]
                symbols.setdefault(name, []).append(loc)

    return {
        "schema": SYMBOLS_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "generated_at": _now_iso(),
        "source_commit": source,
        "ctags_available": available,
        "symbol_count": len(symbols),
        "symbols": symbols,
    }
