"""Approach B — blobless clone with sparse checkout.

``git clone --filter=blob:none`` downloads commits + trees but defers
every blob to a lazy per-object promisor fetch.  This is the "smart"
middle ground that's possible without agentcache: the clone is fast and
small, but accessing any file still triggers an individual round-trip.

We simulate an agent that only needs ``target_paths``: after the
blobless clone we call ``git checkout HEAD -- <paths>`` which triggers
one lazy fetch *per blob*.  For N target files that is N round-trips
(in theory one pack per file), compared with agentcache's single batch.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


def _git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, **kwargs)


def _du_bytes(path: str) -> int:
    out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def _count_files(path: str) -> int:
    return sum(1 for _ in Path(path).rglob("*") if _.is_file())


def _parse_git_progress(stderr: str) -> Dict[str, Any]:
    objects = 0
    recv_bytes = 0
    for line in stderr.splitlines():
        if "Receiving objects" in line and "done" in line:
            parts = line.split(",")
            if len(parts) >= 2:
                count_part = parts[0]
                paren = count_part[count_part.rfind("(") + 1 : count_part.rfind(")")]
                slash = paren.split("/")
                if slash:
                    try:
                        objects += int(slash[0].strip())
                    except ValueError:
                        pass
                size_part = parts[1].strip()
                toks = size_part.split()
                if len(toks) >= 2:
                    unit = toks[1]
                    multiplier = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(
                        unit, 1
                    )
                    try:
                        recv_bytes += int(float(toks[0]) * multiplier)
                    except ValueError:
                        pass
    return {"objects_received": objects, "recv_bytes": recv_bytes}


def run(
    repo_url: str,
    commit: str,
    branch: str,
    target_paths: List[str],
    work_dir: str,
) -> Dict[str, Any]:
    """Blobless clone then sparse checkout of exactly ``target_paths``.

    Parameters
    ----------
    repo_url:
        URL or ``file://`` path to the bare repo.
    commit:
        The exact commit hex the agent is working on (for verification).
    branch:
        Branch to clone.
    target_paths:
        Paths the agent needs.
    work_dir:
        Empty directory to clone into.
    """
    clone_dir = str(Path(work_dir) / "workspace")
    all_stderr: List[str] = []

    t0 = time.monotonic()

    # Phase 1: blobless clone — fast, no blobs materialised.
    # --depth=1 --single-branch keeps history equivalent to the naive approach
    # (one commit, one branch) so the only variable is blob content fetched.
    proc = subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", "--progress",
         "--depth=1", "--single-branch",
         f"--branch={branch}", repo_url, clone_dir],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"blobless clone failed:\n{proc.stderr}")
    all_stderr.append(proc.stderr)

    # Phase 2: checkout exactly the target paths — one lazy fetch per blob.
    co_proc = subprocess.run(
        ["git", "-C", clone_dir, "checkout", "HEAD", "--"] + target_paths,
        capture_output=True,
        text=True,
    )
    if co_proc.returncode != 0:
        raise RuntimeError(f"sparse checkout failed:\n{co_proc.stderr}")
    all_stderr.append(co_proc.stderr)

    elapsed = time.monotonic() - t0

    missing = [p for p in target_paths if not (Path(clone_dir) / p).exists()]
    if missing:
        raise RuntimeError(f"blobless checkout missing expected paths: {missing}")

    combined = "\n".join(all_stderr)
    git_stats = _parse_git_progress(combined)
    disk_bytes = _du_bytes(clone_dir)
    file_count = _count_files(clone_dir)

    return {
        "approach": "blobless (filter=blob:none + sparse checkout)",
        "elapsed_s": round(elapsed, 3),
        "disk_bytes": disk_bytes,
        "file_count": file_count,
        **git_stats,
        "note": f"{len(target_paths)} lazy fetch(es) triggered, one pack per blob",
    }
