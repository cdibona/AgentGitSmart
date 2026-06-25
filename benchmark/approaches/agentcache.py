"""Approach C — the AgentCache flow.

Three phases:
  1. Blobless clone seeded by a bootstrap bundle (optional), or just
     ``--filter=blob:none`` from the promisor.  No content is
     materialised.
  2. POST /cache/<commit>/resolve → the service returns the blob OIDs
     for exactly the paths the agent needs.
  3. ONE ``git fetch origin <oids>`` → a single packfile, no per-blob
     round-trips.

This is the pattern the agentcache design is built around.  The agent
then reads blobs directly by OID (``git cat-file blob <oid>``) without
ever checking out a full working tree.
"""
from __future__ import annotations

import subprocess
import time
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, **kwargs)


def _du_bytes(path: str) -> int:
    out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def _count_files(path: str) -> int:
    return sum(1 for _ in Path(path).rglob("*") if _.is_file())


def _resolve_paths(service_url: str, commit: str, paths: List[str]) -> Dict[str, Any]:
    """POST /cache/<commit>/resolve and return the response dict."""
    url = f"{service_url.rstrip('/')}/cache/{commit}/resolve"
    data = json.dumps({"paths": paths}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"resolve request failed {exc.code}: {body}") from exc


def run(
    repo_url: str,
    commit: str,
    branch: str,
    service_url: str,
    target_paths: List[str],
    work_dir: str,
    bundle_path: Optional[str] = None,
) -> Dict[str, Any]:
    """AgentCache cold start: blobless clone → resolve → targeted fetch.

    Parameters
    ----------
    repo_url:
        URL or ``file://`` path to the bare repo.
    commit:
        Full commit hex that the agentcache service has a cache for.
    branch:
        Branch to clone (so git knows what HEAD is).
    service_url:
        Base URL of the running agentcache Flask service
        (e.g. ``http://127.0.0.1:8765``).
    target_paths:
        Paths the agent needs to read.
    work_dir:
        Empty directory to clone into.
    bundle_path:
        Optional path to a pre-built blobless bootstrap bundle.
        When supplied, ``--bundle-uri`` seeds the clone so the promisor
        transfers almost nothing; leaving it None exercises the plain
        blobless clone path.
    """
    clone_dir = str(Path(work_dir) / "workspace")
    t0 = time.monotonic()

    # ------------------------------------------------------------------
    # Phase 1 — blobless clone (commits + trees, zero blobs)
    # ------------------------------------------------------------------
    clone_cmd = [
        "git", "clone",
        "--filter=blob:none",
        "--no-checkout",
        "--progress",
        f"--branch={branch}",
    ]
    if bundle_path:
        clone_cmd.append(f"--bundle-uri={bundle_path}")
    clone_cmd.extend([repo_url, clone_dir])

    clone_proc = subprocess.run(clone_cmd, capture_output=True, text=True)
    if clone_proc.returncode != 0:
        raise RuntimeError(f"agentcache clone failed:\n{clone_proc.stderr}")
    t_after_clone = time.monotonic()

    # ------------------------------------------------------------------
    # Phase 2 — resolve: ask the service which OIDs we need
    # ------------------------------------------------------------------
    resolved = _resolve_paths(service_url, commit, target_paths)
    oids: List[str] = resolved.get("fetch_oids", [])
    missing: List[str] = resolved.get("missing", [])
    total_bytes: int = resolved.get("total_bytes", 0)
    t_after_resolve = time.monotonic()

    # ------------------------------------------------------------------
    # Phase 3 — one batched fetch of exactly the needed blobs
    # ------------------------------------------------------------------
    if oids:
        fetch_proc = subprocess.run(
            ["git", "-C", clone_dir, "fetch", "--progress", "origin"] + oids,
            capture_output=True,
            text=True,
        )
        if fetch_proc.returncode != 0:
            raise RuntimeError(f"agentcache targeted fetch failed:\n{fetch_proc.stderr}")
    t_after_fetch = time.monotonic()

    elapsed = t_after_fetch - t0

    # Verify we can read each resolved blob by OID (no checkout needed).
    for entry in resolved.get("resolved", []):
        out = subprocess.run(
            ["git", "-C", clone_dir, "cat-file", "blob", entry["oid"]],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            raise RuntimeError(f"cannot read blob {entry['oid']} for {entry['path']}")

    disk_bytes = _du_bytes(clone_dir)
    file_count = _count_files(clone_dir)

    return {
        "approach": "agentcache (blobless → resolve → targeted fetch)",
        "elapsed_s": round(elapsed, 3),
        "phase_clone_s": round(t_after_clone - t0, 3),
        "phase_resolve_s": round(t_after_resolve - t_after_clone, 3),
        "phase_fetch_s": round(t_after_fetch - t_after_resolve, 3),
        "disk_bytes": disk_bytes,
        "file_count": file_count,
        "objects_received": len(oids),
        "recv_bytes": total_bytes,
        "missing_paths": missing,
        "note": f"one packfile; {len(target_paths)} path(s) → {len(oids)} blob(s); "
                f"bundle={'yes' if bundle_path else 'no'}",
    }
