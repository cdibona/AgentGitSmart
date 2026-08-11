"""Write/read the cache artifacts as an orphan commit under a side ref.

The crux of the design. Given artifacts ``{filename: bytes}`` for a source
commit, we:

  1. write each artifact as a blob,
  2. assemble them into a single flat tree (TreeBuilder),
  3. create a *parentless* (orphan) commit pointing at that tree -- so the
     cache contributes no history and is cheap to fetch in isolation,
  4. point ``refs/agent-git-smart/<source-commit>`` at it (force, so regenerating
     is idempotent).

No working tree is ever touched. The query service reads artifacts straight
back out of the object database the same way.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

import pygit2

# pygit2 exposes the filemode constant at top level (with an enums alias in
# newer releases); resolve it defensively so we work across versions.
try:  # pragma: no cover - trivial shim
    _BLOB_MODE = pygit2.GIT_FILEMODE_BLOB
except AttributeError:  # pragma: no cover
    from pygit2.enums import FileMode

    _BLOB_MODE = FileMode.BLOB


def _ref_name(ref_prefix: str, source_commit: str) -> str:
    return f"{ref_prefix.rstrip('/')}/{source_commit}"


def write_cache(
    repo: pygit2.Repository,
    source_commit: str,
    artifacts: Mapping[str, bytes],
    *,
    ref_prefix: str = "refs/agent-git-smart",
    bot_name: str = "AgentGitSmart Bot",
    bot_email: str = "agentgitsmart@localhost",
    message: str | None = None,
) -> Dict[str, Any]:
    """Create the orphan cache commit and (force-)update the side ref."""
    source_commit = str(source_commit)
    builder = repo.TreeBuilder()
    for name, data in sorted(artifacts.items()):
        if "/" in name:
            raise ValueError(f"artifact names must be flat, got {name!r}")
        blob_oid = repo.create_blob(data)
        builder.insert(name, blob_oid, _BLOB_MODE)
    tree_oid = builder.write()

    sig = pygit2.Signature(bot_name, bot_email)
    msg = message or f"agentgitsmart: artifacts for {source_commit}"
    # reference=None -> create the commit object without moving any ref;
    # parents=[] -> orphan, so this never links into the project history.
    commit_oid = repo.create_commit(None, sig, sig, msg, tree_oid, [])

    ref = _ref_name(ref_prefix, source_commit)
    repo.references.create(ref, commit_oid, force=True)

    return {
        "source_commit": source_commit,
        "cache_ref": ref,
        "cache_commit": str(commit_oid),
        "cache_tree": str(tree_oid),
        "artifacts": sorted(artifacts.keys()),
    }


def list_caches(
    repo: pygit2.Repository, ref_prefix: str = "refs/agent-git-smart"
) -> List[str]:
    """Return the source-commit OIDs that currently have a cache ref."""
    prefix = ref_prefix.rstrip("/") + "/"
    out = []
    for name in repo.references:
        if name.startswith(prefix):
            out.append(name[len(prefix) :])
    return sorted(out)


def read_artifact(
    repo: pygit2.Repository,
    source_commit: str,
    name: str,
    *,
    ref_prefix: str = "refs/agent-git-smart",
) -> bytes:
    """Read one artifact's bytes from the side ref. Raises KeyError if absent."""
    ref = _ref_name(ref_prefix, str(source_commit))
    if ref not in repo.references:
        raise KeyError(f"no cache ref for commit {source_commit}")
    commit = repo.references[ref].peel(pygit2.Commit)
    try:
        entry = commit.tree[name]
    except KeyError as exc:
        raise KeyError(f"artifact {name!r} not in cache for {source_commit}") from exc
    return repo[entry.id].data
