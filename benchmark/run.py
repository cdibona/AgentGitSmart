"""Benchmark three agent cold-start approaches.

USAGE — smoke test (no setup required, uses a tiny in-process fixture repo):

    python -m benchmark.run --smoke

USAGE — against a real repo (run setup_repo.sh first):

    python -m benchmark.run \\
        --repo benchmark/repos/cpython.git \\
        --service http://127.0.0.1:8765 \\
        --branch main \\
        --paths Lib/asyncio/tasks.py Lib/ast.py \\
        --runs 3

The service must be running before a full benchmark:

    AGENTGITSMART_REPO_DIR=benchmark/repos/cpython.git \\
        .venv/bin/python -m agentgitsmart.service &

WHAT THIS MEASURES:

  naive    — git clone --depth=1  (all files on disk, like actions/checkout@v4)
  blobless — filter=blob:none then sparse checkout of target paths only
  agentgitsmart — blobless clone + POST /resolve + ONE batched fetch of exact blobs

Output is a comparison table printed to stdout and optionally saved as JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make sure the repo root is on the path when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.approaches import naive, blobless, agentgitsmart as ac
from agentgitsmart.config import AgentGitSmartConfig
from agentgitsmart.hook import generate_for_commit
import pygit2


# ---------------------------------------------------------------------------
# Smoke-test fixture helpers
# ---------------------------------------------------------------------------


def _create_smoke_repo(dest: str) -> tuple[pygit2.Repository, str]:
    """Create a tiny fixture bare repo for smoke testing.

    Mirrors the contents from tests/conftest.py so we don't need to
    import from the tests tree (which may not be on sys.path in all
    invocations).
    """
    from collections import defaultdict

    FILES = {
        "src/app.py": (
            '"""Token refresh helpers."""\n'
            "from __future__ import annotations\n\n\n"
            "class TokenRefresher:\n"
            "    def __init__(self, client_id: str, client_secret: str) -> None:\n"
            "        self.client_id = client_id\n"
            "        self.client_secret = client_secret\n"
            '        self._token: str = ""\n\n'
            "    def refresh(self) -> str:\n"
            '        self._token = "refreshed"\n'
            "        return self._token\n\n\n"
            "def make_refresher(client_id: str, client_secret: str) -> 'TokenRefresher':\n"
            "    return TokenRefresher(client_id, client_secret)\n"
        ),
        "src/util.c": (
            "#include <string.h>\n\n"
            "size_t str_len(const char *s) { return strlen(s); }\n"
        ),
        "README.md": "# smoke fixture\n",
    }

    _BLOB = 0o100644
    _TREE = 0o040000

    def _build(repo, files):
        root_f, subs = {}, defaultdict(dict)
        for p, v in files.items():
            head, _, tail = p.partition("/")
            if tail:
                subs[head][tail] = v
            else:
                root_f[head] = v
        b = repo.TreeBuilder()
        for n in sorted(root_f):
            b.insert(n, repo.create_blob(root_f[n].encode()), _BLOB)
        for d in sorted(subs):
            b.insert(d, _build(repo, subs[d]), _TREE)
        return b.write()

    r = pygit2.init_repository(dest, bare=True)
    tree_oid = _build(r, FILES)
    sig = pygit2.Signature("Bench", "bench@local")
    commit_oid = r.create_commit("refs/heads/master", sig, sig,
                                 "Initial commit\n", tree_oid, [])
    return r, str(commit_oid)


# ---------------------------------------------------------------------------
# Service lifecycle helpers
# ---------------------------------------------------------------------------


def _start_service(repo_dir: str, port: int) -> subprocess.Popen:
    """Start the agentgitsmart Flask service as a child process."""
    env = dict(os.environ)
    env["AGENTGITSMART_REPO_DIR"] = repo_dir
    env["AGENTGITSMART_SERVICE_PORT"] = str(port)
    env["AGENTGITSMART_SERVICE_HOST"] = "127.0.0.1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentgitsmart.service"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give Flask a moment to bind.
    time.sleep(0.8)
    return proc


def _stop_service(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def _wait_for_service(url: str, timeout: float = 10.0) -> bool:
    """Poll the /healthz endpoint until it responds or times out."""
    import urllib.request
    import urllib.error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{url}/healthz", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} TiB"


def _fmt_s(s: float) -> str:
    return f"{s:.3f}s"


def _print_table(results: List[Dict[str, Any]]) -> None:
    cols = [
        ("Approach",          lambda r: r["approach"]),
        ("Wall time",         lambda r: _fmt_s(r["elapsed_s"])),
        ("Recv bytes",        lambda r: _fmt_bytes(r.get("recv_bytes", 0))),
        ("Objects recv",      lambda r: str(r.get("objects_received", "—"))),
        ("Disk (clone dir)",  lambda r: _fmt_bytes(r.get("disk_bytes", 0))),
        ("Files on disk",     lambda r: str(r.get("file_count", "—"))),
        ("Note",              lambda r: r.get("note", "")),
    ]
    widths = [max(len(h), max((len(fn(r)) for r in results), default=0)) for h, fn in cols]

    sep = "  "
    header = sep.join(h.ljust(w) for (h, _), w in zip(cols, widths))
    rule = sep.join("-" * w for w in widths)
    print()
    print(header)
    print(rule)
    for r in results:
        row = sep.join(fn(r).ljust(w) for (_, fn), w in zip(cols, widths))
        print(row)
    print()


# ---------------------------------------------------------------------------
# Main benchmark logic
# ---------------------------------------------------------------------------


def _run_one(
    approach_name: str,
    repo_url: str,
    commit: str,
    branch: str,
    target_paths: List[str],
    service_url: str,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agentgitsmart-bench-") as work_dir:
        if approach_name == "naive":
            return naive.run(repo_url, branch, target_paths, work_dir)
        elif approach_name == "blobless":
            return blobless.run(repo_url, commit, branch, target_paths, work_dir)
        elif approach_name == "agentgitsmart":
            return ac.run(repo_url, commit, branch, service_url, target_paths, work_dir)
        else:
            raise ValueError(f"Unknown approach: {approach_name!r}")


def _aggregate(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Average numeric fields across runs; keep the last non-numeric fields."""
    if not runs:
        return {}
    merged = dict(runs[-1])
    numeric_keys = [k for k, v in runs[0].items() if isinstance(v, (int, float))]
    for k in numeric_keys:
        vals = [r[k] for r in runs if k in r]
        merged[k] = sum(vals) / len(vals) if vals else 0
    return merged


def run_benchmark(
    repo_path: str,
    commit: str,
    branch: str,
    target_paths: List[str],
    service_url: str,
    approaches: List[str],
    runs: int,
) -> List[Dict[str, Any]]:
    repo_url = "file://" + os.path.abspath(repo_path)
    results = []
    for approach in approaches:
        print(f"  [{approach}] running {runs} time(s)...", end="", flush=True)
        run_results = []
        for i in range(runs):
            try:
                r = _run_one(approach, repo_url, commit, branch, target_paths, service_url)
                run_results.append(r)
                print(".", end="", flush=True)
            except Exception as exc:
                print(f"\n    ERROR in run {i+1}: {exc}")
        print()
        if run_results:
            results.append(_aggregate(run_results))
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true",
                    help="Run against a tiny in-process fixture repo (no setup required).")
    ap.add_argument("--repo", metavar="PATH",
                    help="Path to the local bare git repo (required unless --smoke).")
    ap.add_argument("--branch", default="master",
                    help="Branch to benchmark against (default: master).")
    ap.add_argument("--paths", nargs="+", metavar="PATH",
                    help="Files the 'agent' needs (relative to repo root).")
    ap.add_argument("--service", metavar="URL", default="http://127.0.0.1:8765",
                    help="Running agentgitsmart service URL (default: http://127.0.0.1:8765).")
    ap.add_argument("--start-service", action="store_true",
                    help="Start the agentgitsmart service automatically (uses --repo).")
    ap.add_argument("--approaches", nargs="+",
                    choices=["naive", "blobless", "agentgitsmart"],
                    default=["naive", "blobless", "agentgitsmart"],
                    help="Approaches to benchmark.")
    ap.add_argument("--runs", type=int, default=3,
                    help="Number of timed runs per approach (results are averaged).")
    ap.add_argument("--output", metavar="FILE",
                    help="Write JSON results to FILE.")
    args = ap.parse_args(argv)

    service_proc: Optional[subprocess.Popen] = None

    try:
        if args.smoke:
            # ----------------------------------------------------------------
            # Smoke mode: create a tiny repo in a temp dir, generate cache,
            # spin up the service, and run all three approaches.
            # ----------------------------------------------------------------
            print("=== agentgitsmart smoke benchmark ===")
            print("Creating fixture repo...")
            smoke_tmp = tempfile.mkdtemp(prefix="agentgitsmart-smoke-")
            repo_path = os.path.join(smoke_tmp, "smoke.git")
            r, commit = _create_smoke_repo(repo_path)
            r.config["uploadpack.allowFilter"] = "true"
            r.config["uploadpack.allowanysha1inwant"] = "true"

            branch = "master"
            target_paths = ["src/app.py"]

            cfg = AgentGitSmartConfig(repo_dir=r.path)
            print(f"Generating agentgitsmart artifacts for {commit[:8]}...")
            generate_for_commit(r, commit, cfg)

            port = 8765
            service_url = f"http://127.0.0.1:{port}"
            print("Starting agentgitsmart service...")
            service_proc = _start_service(r.path, port)
            if not _wait_for_service(service_url):
                print("ERROR: service did not start in time", file=sys.stderr)
                return 1

            print(f"Benchmarking ({args.runs} run(s) each, target: {target_paths})...")

        else:
            # ----------------------------------------------------------------
            # Full mode: user-supplied repo.
            # ----------------------------------------------------------------
            if not args.repo:
                ap.error("--repo is required unless --smoke is specified.")
            if not args.paths:
                ap.error("--paths is required unless --smoke is specified.")

            repo_path = args.repo
            branch = args.branch
            target_paths = args.paths
            service_url = args.service

            print("=== agentgitsmart benchmark ===")
            print(f"Repo:   {repo_path}")
            print(f"Branch: {branch}")
            print(f"Paths:  {target_paths}")
            print(f"Runs:   {args.runs}")

            r = pygit2.Repository(repo_path)
            ref = r.references[f"refs/heads/{branch}"]
            commit = str(ref.peel(pygit2.Commit).id)
            print(f"Commit: {commit[:12]}")

            if args.start_service:
                port = int(service_url.split(":")[-1]) if ":" in service_url else 8765
                print(f"Starting agentgitsmart service on port {port}...")
                service_proc = _start_service(repo_path, port)
                if not _wait_for_service(service_url):
                    print("ERROR: service did not start", file=sys.stderr)
                    return 1
            else:
                # Verify the service is reachable before we start timing.
                if not _wait_for_service(service_url, timeout=2):
                    print(
                        f"WARNING: agentgitsmart service not reachable at {service_url}.\n"
                        "  Start it with:\n"
                        f"    AGENTGITSMART_REPO_DIR={repo_path} "
                        f".venv/bin/python -m agentgitsmart.service\n"
                        "  Or pass --start-service to have this script do it.",
                        file=sys.stderr,
                    )
                    if "agentgitsmart" in args.approaches:
                        print("  Dropping 'agentgitsmart' approach from benchmark.")
                        args.approaches = [a for a in args.approaches if a != "agentgitsmart"]

            print(f"Benchmarking ({args.runs} run(s) each)...")

        results = run_benchmark(
            repo_path, commit, branch, target_paths,
            service_url, args.approaches, args.runs,
        )

        print("\n=== Results ===")
        _print_table(results)

        if args.output:
            Path(args.output).write_text(json.dumps(results, indent=2))
            print(f"Results saved to {args.output}")

    finally:
        if service_proc is not None:
            _stop_service(service_proc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
