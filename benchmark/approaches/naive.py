"""Approach A — naive shallow clone.

This is what ``actions/checkout@v4`` does by default: a depth-1 clone
that materialises every tracked file regardless of whether the agent
touches them.  On a small repo the numbers look fine; on a repo the
size of CPython (~300 MB, 60 k+ files) it's the expensive baseline.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


def _git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, **kwargs)


def _du_bytes(path: str) -> int:
    """Total size of ``path`` in bytes (follows symlinks for regular files)."""
    out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def _count_files(path: str) -> int:
    return sum(1 for _ in Path(path).rglob("*") if _.is_file())


def _parse_git_progress(stderr: str) -> Dict[str, Any]:
    """Pull objects-received and byte count out of ``git clone --progress`` output."""
    objects = 0
    recv_bytes = 0
    for line in stderr.splitlines():
        if "Receiving objects" in line and "done" in line:
            # e.g. "Receiving objects: 100% (1234/5678), 42.12 MiB | ..., done."
            parts = line.split(",")
            if len(parts) >= 2:
                count_part = parts[0]  # "Receiving objects: 100% (1234/5678)"
                paren = count_part[count_part.rfind("(") + 1 : count_part.rfind(")")]
                with_slash = paren.split("/")
                if with_slash:
                    try:
                        objects = int(with_slash[0].strip())
                    except ValueError:
                        pass
                size_part = parts[1].strip()  # "42.12 MiB | ..."
                size_tok = size_part.split()[0]
                unit = size_part.split()[1] if len(size_part.split()) > 1 else "B"
                try:
                    size_f = float(size_tok)
                    multiplier = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(unit, 1)
                    recv_bytes = int(size_f * multiplier)
                except ValueError:
                    pass
    return {"objects_received": objects, "recv_bytes": recv_bytes}


def run(
    repo_url: str,
    branch: str,
    target_paths: List[str],
    work_dir: str,
) -> Dict[str, Any]:
    """Clone depth=1 (all blobs), then verify target paths are on disk.

    Parameters
    ----------
    repo_url:
        URL or ``file://`` path to the bare repo (the "server").
    branch:
        Branch name to clone (e.g. ``main`` or ``master``).
    target_paths:
        Paths the agent needs to read (used only to verify they landed on disk).
    work_dir:
        Empty directory to clone into.
    """
    clone_dir = str(Path(work_dir) / "workspace")

    t0 = time.monotonic()
    proc = subprocess.run(
        ["git", "clone", "--depth=1", "--progress",
         f"--branch={branch}", repo_url, clone_dir],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"naive clone failed:\n{proc.stderr}")
    elapsed = time.monotonic() - t0

    # Verify all target files landed on disk (they always do with a full checkout).
    missing = [p for p in target_paths if not (Path(clone_dir) / p).exists()]
    if missing:
        raise RuntimeError(f"naive clone missing expected paths: {missing}")

    git_stats = _parse_git_progress(proc.stderr)
    disk_bytes = _du_bytes(clone_dir)
    file_count = _count_files(clone_dir)

    return {
        "approach": "naive (depth=1 clone)",
        "elapsed_s": round(elapsed, 3),
        "disk_bytes": disk_bytes,
        "file_count": file_count,
        **git_stats,
        "note": "every tracked file materialized; agent needs only subset",
    }
