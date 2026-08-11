"""Simulate realistic agent work after a clone, for each approach.

Stdlib-only: importable inside Docker containers that only have git + python3.

``blobless_batch`` (AgentGitSmartBlobless) reuses the ``blobless`` post-clone task:
the two differ only in HOW the clone fetched content (N lazy fetches vs one
batched fetch), not in the agent-side read/grep work simulated here.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional


def _rusage_children_cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def _git(*args: str, cwd: Optional[str] = None, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, timeout=timeout, check=check)


def _http_get(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": str(e), "status": e.code}
    except Exception as e:
        return {"error": str(e)}


def run_agent_task(
    approach: str,
    workspace: str,
    commit: str,
    target_paths: list[str],
    service_url: str,
    symbol: str = "ClassDef",
) -> dict:
    """
    Simulate agent work after the clone. Returns AgentTaskMetrics-shaped dict plus 'detail'.
    Must complete in < 30s.
    """
    t_start = time.monotonic()

    result = {
        "symbol_lookup_ms": 0.0,
        "file_read_ms": 0.0,
        "network_roundtrips": 0,
        "total_agent_ready_ms": 0.0,
        "grep_cpu_pct": 0.0,
        "detail": {"approach": approach, "files_read": 0, "symbol_hits": 0, "notes": ""},
    }

    try:
        if approach == "naive":
            _task_naive(workspace, target_paths, symbol, result)
        elif approach in ("blobless", "blobless_batch"):
            _task_blobless(workspace, target_paths, symbol, result)
        elif approach == "agentgitsmart":
            _task_agentgitsmart(workspace, commit, target_paths, service_url, symbol, result)
    except Exception as e:
        result["detail"]["notes"] += f" ERROR: {e}"

    result["total_agent_ready_ms"] = round((time.monotonic() - t_start) * 1000.0, 1)
    return result


def _task_naive(workspace: str, target_paths: list[str], symbol: str, result: dict) -> None:
    """Naive: all files on disk. Read targets, then grep all .py/.c/.h files for symbol."""
    # Task 1: read target files from disk (fast — already present)
    t0 = time.monotonic()
    files_read = 0
    for p in target_paths:
        full = os.path.join(workspace, p)
        if os.path.exists(full):
            with open(full, "rb") as f:
                f.read()
            files_read += 1
    result["detail"]["files_read"] = files_read

    # Task 2: symbol search — grep all files (CPU-heavy; this is intentional)
    cpu_before = _rusage_children_cpu_s()
    wall_before = time.monotonic()
    proc = subprocess.run(
        ["grep", "-r", "-l", f"class {symbol}", workspace],
        capture_output=True, text=True, timeout=25, check=False,
    )
    wall_after = time.monotonic()
    cpu_after = _rusage_children_cpu_s()

    wall_s = wall_after - wall_before
    cpu_s = cpu_after - cpu_before
    result["grep_cpu_pct"] = round((cpu_s / wall_s * 100.0) if wall_s > 0 else 0.0, 1)
    result["symbol_lookup_ms"] = round(wall_s * 1000.0, 1)
    hits = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    result["detail"]["symbol_hits"] = len(hits)
    result["network_roundtrips"] = 0

    # Task 3: read 3 more files from disk (already present)
    for p in (hits[:3] if hits else []):
        fp = p.strip()
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                f.read()
    result["file_read_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    result["detail"]["notes"] = f"grepped workspace; {len(hits)} files contain '{symbol}'"


def _task_blobless(workspace: str, target_paths: list[str], symbol: str, result: dict) -> None:
    """Blobless: target files checked out. Grep is cheap (few files). Other files need lazy fetch."""
    # Task 1: read checked-out target files
    t0 = time.monotonic()
    files_read = 0
    for p in target_paths:
        full = os.path.join(workspace, p)
        if os.path.exists(full):
            with open(full, "rb") as f:
                f.read()
            files_read += 1

    # Task 2: grep only checked-out files (fast but incomplete)
    cpu_before = _rusage_children_cpu_s()
    w0 = time.monotonic()
    proc = subprocess.run(
        ["grep", "-r", "-l", f"class {symbol}", workspace],
        capture_output=True, text=True, timeout=10, check=False,
    )
    result["symbol_lookup_ms"] = round((time.monotonic() - w0) * 1000.0, 1)
    cpu_after = _rusage_children_cpu_s()
    wall_s = time.monotonic() - w0
    result["grep_cpu_pct"] = round(((cpu_after - cpu_before) / wall_s * 100.0) if wall_s > 0 else 0.0, 1)
    hits = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    result["detail"]["symbol_hits"] = len(hits)

    # Task 3: access 3 additional files NOT in target_paths → lazy promisor fetches
    # Get a list of repo paths via git ls-tree
    roundtrips = 0
    try:
        ls = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=workspace, timeout=10, check=False)
        all_paths = [ln.strip() for ln in ls.stdout.splitlines() if ln.strip().endswith(".py")]
        extras = [p for p in all_paths if p not in target_paths][:3]
        for extra in extras:
            # cat-file HEAD:path triggers a promisor fetch for blobless clones
            r = subprocess.run(
                ["git", "cat-file", "blob", f"HEAD:{extra}"],
                capture_output=True, cwd=workspace, timeout=15, check=False,
            )
            if r.returncode == 0:
                files_read += 1
                roundtrips += 1
        result["file_read_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    except Exception as e:
        result["detail"]["notes"] += f" ls-tree error: {e}"

    result["network_roundtrips"] = roundtrips
    result["detail"]["files_read"] = files_read
    result["detail"]["notes"] = (
        f"grep on {len(target_paths)} checked-out file(s); "
        f"{roundtrips} lazy promisor fetch(es) for additional files (incomplete — only checked-out files searchable)"
    )


def _task_agentgitsmart(workspace: str, commit: str, target_paths: list[str],
                     service_url: str, symbol: str, result: dict) -> None:
    """AgentGitSmart: use service for symbol lookup, batch fetch needed blobs."""
    roundtrips = 0

    # Task 1: symbol lookup via service (1 HTTP round-trip)
    t0 = time.monotonic()
    url = f"{service_url.rstrip('/')}/cache/{commit}/symbol/{symbol}"
    sym_resp = _http_get(url)
    result["symbol_lookup_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    roundtrips += 1

    oids = sym_resp.get("fetch_oids", [])
    hits = len(sym_resp.get("locations", []))
    result["detail"]["symbol_hits"] = hits

    # Task 2: batch fetch all needed blobs in ONE git fetch (1 round-trip)
    t2 = time.monotonic()
    files_read = 0
    if oids:
        subprocess.run(
            ["git", "-c", "remote.origin.partialclonefilter=",
             "fetch", "origin"] + oids[:5],  # cap at 5 to bound time
            capture_output=True, text=True, cwd=workspace, timeout=20, check=False,
        )
        roundtrips += 1

        # Task 3: read blobs by OID — no network, already fetched
        for oid in oids[:5]:
            r = subprocess.run(
                ["git", "cat-file", "blob", oid],
                capture_output=True, cwd=workspace, timeout=10, check=False,
            )
            if r.returncode == 0:
                files_read += 1

    result["file_read_ms"] = round((time.monotonic() - t2) * 1000.0, 1)
    result["network_roundtrips"] = roundtrips
    result["detail"]["files_read"] = files_read
    result["detail"]["notes"] = (
        f"service symbol lookup returned {hits} location(s), {len(oids)} OID(s); "
        f"batch fetch of {min(len(oids),5)} blob(s); {roundtrips} total round-trip(s)"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--approach", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--service-url", default="http://127.0.0.1:8765")
    p.add_argument("--symbol", default="ClassDef")
    p.add_argument("--target", action="append", dest="targets", default=[])
    args = p.parse_args(argv)
    result = run_agent_task(args.approach, args.workspace, args.commit,
                            args.targets, args.service_url, args.symbol)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
