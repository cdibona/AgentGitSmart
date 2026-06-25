"""
PackCache Real Agent — stdlib-only.

Task: Add a '!' to one comment line in 1% of the project's Python files,
then commit the change locally (no push — we benchmark the COST of
discovering and reading those files, not write-back latency).

The three approaches differ only in HOW the agent acquires the files it
needs to read and edit:

  naive:      git clone --depth=1  →  all files on disk, no planning needed
  blobless:   filter=blob:none + depth=1  →  N lazy fetches, one per file
  agentcache: filter=blob:none (full history)  →  manifest for planning,
              POST /resolve → ONE batch fetch of exactly the needed files

Stdlib-only so it runs inside the Docker container (python3 + git only).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Optional

SEED_DEFAULT = 42
CLONE_TIMEOUT = 360  # full blobless clone of cpython can take a minute or two


# ── git helpers ────────────────────────────────────────────────────────────

def _git(
    *args: str,
    cwd: Optional[str] = None,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=check,
    )


# ── agentcache helpers ─────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 10) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(url: str, data: dict, timeout: int = 30) -> Optional[dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _detect_agentcache(service_url: str, commit: str) -> Optional[list]:
    """Return manifest entries if service is reachable, else None."""
    data = _http_get(
        f"{service_url.rstrip('/')}/cache/{commit}/manifest", timeout=5
    )
    return data.get("entries") if (data and "entries" in data) else None


# ── comment editing ────────────────────────────────────────────────────────

def _add_exclamation(content: str, rng: random.Random) -> tuple:
    """Add '!' to a randomly chosen comment line. Returns (new_content, bool)."""
    lines = content.split("\n")
    eligible = [
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("#") and not line.rstrip().endswith("!")
    ]
    if not eligible:
        return content, False
    idx = rng.choice(eligible)
    lines[idx] = lines[idx].rstrip() + "!"
    return "\n".join(lines), True


# ── main agent logic ───────────────────────────────────────────────────────

def run_real_agent(
    approach: str,
    repo_url: str,
    commit: str,
    branch: str,
    service_url: str = "http://127.0.0.1:8765",
    pct: float = 1.0,
    seed: int = SEED_DEFAULT,
) -> dict:
    """
    Run the real agent task end-to-end and return a metrics dict.

    The returned dict includes per-phase timing breakdowns, round-trip
    counts, files modified, and the local commit SHA as proof of work.
    """
    rng = random.Random(seed)
    t_start = time.monotonic()

    metrics: dict = {
        "approach": approach,
        "agentcache_detected": False,
        "files_found": 0,
        "files_selected": 0,
        "files_fetched": 0,
        "files_modified": 0,
        "comments_modified": 0,
        "fetch_roundtrips": 0,
        "commit_sha": None,
        "phase_clone_ms": 0.0,
        "phase_discover_ms": 0.0,
        "phase_fetch_ms": 0.0,
        "phase_edit_ms": 0.0,
        "phase_commit_ms": 0.0,
        "elapsed_ms": 0.0,
        "error": None,
    }

    work_dir = tempfile.mkdtemp(prefix="real-agent-")
    clone_dir = os.path.join(work_dir, "repo")

    try:
        # ── Phase 1: Clone ─────────────────────────────────────────
        t0 = time.monotonic()
        if approach == "naive":
            # Depth-1 gives every tracked file immediately — like actions/checkout.
            _git("clone", "--depth=1", f"--branch={branch}",
                 repo_url, clone_dir, timeout=CLONE_TIMEOUT)

        elif approach == "blobless":
            # Shallow blobless: smallest possible clone (commits + trees, no blobs).
            _git("clone", "--filter=blob:none", "--depth=1", "--no-checkout",
                 f"--branch={branch}", repo_url, clone_dir, timeout=CLONE_TIMEOUT)

        else:  # agentcache — full blobless, no depth (so targeted OID fetch works)
            _git("clone", "--filter=blob:none", "--no-checkout",
                 f"--branch={branch}", repo_url, clone_dir, timeout=CLONE_TIMEOUT)

        metrics["phase_clone_ms"] = round((time.monotonic() - t0) * 1000, 1)

        # ── Phase 2: Discover Python files ─────────────────────────
        t1 = time.monotonic()
        all_py: list = []

        if approach == "agentcache":
            # Ask the service for the manifest — no file content needed.
            entries = _detect_agentcache(service_url, commit)
            if entries is not None:
                metrics["agentcache_detected"] = True
                all_py = [
                    e["path"] for e in entries if e["path"].endswith(".py")
                ]
            else:
                # Service down: fall back to git ls-tree (still blobless).
                r = _git("ls-tree", "-r", "--name-only", "HEAD",
                         cwd=clone_dir, timeout=30)
                all_py = [l for l in r.stdout.splitlines() if l.endswith(".py")]

        elif approach == "blobless":
            # ls-tree works because we have all tree objects.
            r = _git("ls-tree", "-r", "--name-only", "HEAD",
                     cwd=clone_dir, timeout=30)
            all_py = [l for l in r.stdout.splitlines() if l.endswith(".py")]

        else:  # naive — walk the working tree on disk
            for root, dirs, files in os.walk(clone_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    if fname.endswith(".py"):
                        rel = os.path.relpath(
                            os.path.join(root, fname), clone_dir
                        )
                        all_py.append(rel)

        all_py.sort()
        metrics["files_found"] = len(all_py)
        metrics["phase_discover_ms"] = round((time.monotonic() - t1) * 1000, 1)

        if not all_py:
            raise RuntimeError("No Python files found in repo")

        # ── Phase 3: Select 1% ─────────────────────────────────────
        n_select = max(1, int(len(all_py) * pct / 100))
        selected = sorted(rng.sample(all_py, min(n_select, len(all_py))))
        metrics["files_selected"] = len(selected)

        # ── Phase 4: Fetch selected files ──────────────────────────
        t2 = time.monotonic()

        if approach == "naive":
            # Nothing to do — files are already on disk.
            pass

        elif approach == "agentcache" and metrics["agentcache_detected"]:
            # Resolve paths → OIDs → ONE batch git fetch.
            resolved = _http_post(
                f"{service_url.rstrip('/')}/cache/{commit}/resolve",
                {"paths": selected},
                timeout=60,
            )
            oids = resolved.get("fetch_oids", []) if resolved else []
            if oids:
                _git(
                    "-c", "remote.origin.partialclonefilter=",
                    "fetch", "origin", *oids,
                    cwd=clone_dir, timeout=120,
                )
                metrics["fetch_roundtrips"] = 1
            # Materialise selected files into the working tree from the
            # now-local blobs (no more network after this).
            _git("checkout", "HEAD", "--", *selected,
                 cwd=clone_dir, timeout=30)

        else:
            # blobless or agentcache-fallback: one lazy promisor fetch per file.
            for path in selected:
                _git("checkout", "HEAD", "--", path,
                     cwd=clone_dir, timeout=30, check=False)
                metrics["fetch_roundtrips"] += 1

        metrics["phase_fetch_ms"] = round((time.monotonic() - t2) * 1000, 1)

        # ── Phase 5: Edit — add ! to one comment per file ──────────
        t3 = time.monotonic()
        n_modified = 0
        n_on_disk = 0

        for path in selected:
            full = os.path.join(clone_dir, path)
            if not os.path.exists(full):
                continue
            n_on_disk += 1
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                new_content, changed = _add_exclamation(content, rng)
                if changed:
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(new_content)
                    n_modified += 1
            except Exception:
                pass

        metrics["files_fetched"] = n_on_disk
        metrics["files_modified"] = n_modified
        metrics["comments_modified"] = n_modified
        metrics["phase_edit_ms"] = round((time.monotonic() - t3) * 1000, 1)

        # ── Phase 6: Commit ────────────────────────────────────────
        t4 = time.monotonic()
        if n_modified > 0:
            _git("config", "user.email", "agent@packcache", cwd=clone_dir)
            _git("config", "user.name", "PackCache Real Agent", cwd=clone_dir)
            _git("add", "-u", cwd=clone_dir, timeout=30)
            _git(
                "commit", "-m",
                (
                    f"agent({approach}): add ! to {n_modified} comment(s)\n\n"
                    f"approach={approach} agentcache={metrics['agentcache_detected']} "
                    f"seed={seed} pct={pct}%"
                ),
                cwd=clone_dir,
                timeout=30,
            )
            sha = _git("rev-parse", "HEAD", cwd=clone_dir)
            metrics["commit_sha"] = sha.stdout.strip()

        metrics["phase_commit_ms"] = round((time.monotonic() - t4) * 1000, 1)

    except Exception as exc:
        metrics["error"] = str(exc)

    metrics["elapsed_ms"] = round((time.monotonic() - t_start) * 1000, 1)
    return metrics


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--approach", required=True,
                   choices=["naive", "blobless", "agentcache"])
    p.add_argument("--repo-url", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--service-url", default="http://127.0.0.1:8765")
    p.add_argument("--pct", type=float, default=1.0,
                   help="Percentage of .py files to modify (default: 1.0)")
    p.add_argument("--seed", type=int, default=SEED_DEFAULT)
    args = p.parse_args(argv)

    result = run_real_agent(
        approach=args.approach,
        repo_url=args.repo_url,
        commit=args.commit,
        branch=args.branch,
        service_url=args.service_url,
        pct=args.pct,
        seed=args.seed,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
