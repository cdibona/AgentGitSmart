"""Approach B2 — blobless clone + ONE batched fetch, NO server (AgentGitSmartBlobless).

This is the "AgentGitSmartBlobless" arm: it takes the same ``blobless`` clone as
:mod:`benchmark.approaches.blobless`, but instead of ``git checkout HEAD -- <paths>``
(which lazily fetches one blob per file, N round-trips) it reads the target
paths' blob OIDs straight from the LOCAL trees a blobless clone already has
(``git ls-tree``, zero content fetched) and fetches them all in a SINGLE
``git fetch origin <oids>``.

The point: this captures agentgitsmart's batched-fetch win with stock git alone —
no post-receive hook, no query service, no side ref, no symbol index. Comparing
it against the full ``agentgitsmart`` arm isolates exactly what the server buys you
*over* a competent blobless client.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

# SHA-1 of a zero-byte file — git's intrinsic "empty blob". Servers reject a
# `want` for it, which would fail the whole batch, so we drop it from the OID
# list (it is always already present locally anyway).
EMPTY_BLOB_OID = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def _du_bytes(path: str) -> int:
    out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def _count_files(path: str) -> int:
    return sum(1 for _ in Path(path).rglob("*") if _.is_file())


def _oids_for_paths(clone_dir: str, target_paths: List[str]) -> Dict[str, str]:
    """Read path→blob-OID for *target_paths* from the LOCAL trees (no content fetch).

    ``git ls-tree -r HEAD`` walks tree objects only — every one is present after
    a blobless clone, so this transfers zero bytes.  We deliberately do NOT pass
    ``-l`` (show size): blob sizes are not local under a blobless clone, so ``-l``
    would lazily fetch every blob just to report its size.
    """
    want = set(target_paths)
    out: Dict[str, str] = {}
    r = subprocess.run(
        ["git", "-C", clone_dir, "ls-tree", "-r", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    for raw in r.stdout.splitlines():
        # <mode> <type> <oid>\t<path>
        if "\t" not in raw:
            continue
        meta, path = raw.split("\t", 1)
        if path not in want:
            continue
        parts = meta.split()
        if len(parts) >= 3:
            out[path] = parts[2]
    return out


def run(
    repo_url: str,
    commit: str,
    branch: str,
    target_paths: List[str],
    work_dir: str,
) -> Dict[str, Any]:
    """Blobless clone, then ONE batched fetch of exactly ``target_paths`` by OID."""
    clone_dir = str(Path(work_dir) / "workspace")

    t0 = time.monotonic()

    # Phase 1: blobless clone — identical to the blobless arm.
    proc = subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", "--progress",
         "--depth=1", "--single-branch",
         f"--branch={branch}", repo_url, clone_dir],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"blobless clone failed:\n{proc.stderr}")

    # Phase 2: read target OIDs from local trees, then ONE batched fetch.
    path_oid = _oids_for_paths(clone_dir, target_paths)
    oids = sorted({o for o in path_oid.values() if o and o != EMPTY_BLOB_OID})
    roundtrips = 0
    if oids:
        subprocess.run(
            ["git", "-C", clone_dir, "fetch", "--no-tags",
             "--no-write-fetch-head", "origin", *oids],
            capture_output=True, text=True, check=False,
        )
        roundtrips = 1

    # Materialise the target files from the now-local blobs.
    co_proc = subprocess.run(
        ["git", "-C", clone_dir, "checkout", "HEAD", "--"] + target_paths,
        capture_output=True,
        text=True,
    )
    if co_proc.returncode != 0:
        raise RuntimeError(f"batched checkout failed:\n{co_proc.stderr}")

    elapsed = time.monotonic() - t0

    missing = [p for p in target_paths if not (Path(clone_dir) / p).exists()]
    if missing:
        raise RuntimeError(f"blobless_batch checkout missing expected paths: {missing}")

    return {
        "approach": "blobless_batch (filter=blob:none + ONE batched fetch, no server)",
        "elapsed_s": round(elapsed, 3),
        "disk_bytes": _du_bytes(clone_dir),
        "file_count": _count_files(clone_dir),
        "objects_received": len(oids),
        "note": (
            f"{len(oids)} blob(s) resolved from local trees and fetched in "
            f"{roundtrips} batched round-trip(s) — no server"
        ),
    }
