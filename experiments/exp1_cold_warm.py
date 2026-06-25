"""Experiment 1 — Cold vs warm cache across the five projects.

For each repo:
  1. Erase any existing agentcache artifacts (start from nothing).
  2. For each method (naive, blobless, agentcache):
       run the 2%-of-files agentic task N times (default 5).

What it shows:
  - naive / blobless have no cache, so all N runs are a flat baseline.
  - agentcache run #1 is the "expensive first visit": it triggers the
    server to build the cache (lazy generation).  Runs #2..N are "warm":
    the cache already exists, so the resolve is instant.

Output: experiments/results/exp1_cold_warm.json + a printed table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .harness import ExperimentHarness, ALL_REPOS, fmt_bytes

RESULTS = Path(__file__).resolve().parent / "results"
METHODS = ["naive", "blobless", "agentcache"]


async def run(repos, iterations: int, pct: float, base_seed: int) -> dict:
    h = ExperimentHarness()
    await h.start()
    runs: list[dict] = []
    try:
        for repo in repos:
            removed = h.clear_cache(repo, gc=False)
            print(f"\n=== {repo} === (cleared {removed} pre-existing cache ref(s))")
            for method in METHODS:
                for i in range(1, iterations + 1):
                    seed = base_seed + i
                    res = await h.run_agent(
                        repo, method, iteration=i, seed=seed, pct=pct
                    )
                    runs.append(res.to_dict())
                    tag = ""
                    if method == "agentcache":
                        tag = "COLD-build" if res.cache_built_this_run else "warm"
                    err = f" ERROR={res.error[:40]}" if res.error else ""
                    print(
                        f"  {method:11} run{i} seed={seed} "
                        f"net={fmt_bytes(res.bytes_proxy_out):>10} "
                        f"wall={res.wall_s:5.2f}s "
                        f"fetch={res.phase_fetch_ms:6.0f}ms "
                        f"mod={res.files_modified:3d} {tag}{err}"
                    )
    finally:
        await h.stop()

    report = {
        "experiment": "cold_warm",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "iterations": iterations,
        "pct": pct,
        "repos": repos,
        "runs": runs,
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "exp1_cold_warm.json"
    out.write_text(json.dumps(report, indent=2))
    _summarize(report)
    print(f"\nWrote {out}")
    return report


def _summarize(report: dict) -> None:
    """Print the cold-vs-warm contrast for agentcache, per repo."""
    print("\n" + "=" * 72)
    print("SUMMARY — agentcache cold (first visit) vs warm (subsequent)")
    print("=" * 72)
    runs = report["runs"]
    for repo in report["repos"]:
        ac = [r for r in runs if r["repo"] == repo and r["method"] == "agentcache"]
        if not ac:
            continue
        cold = next((r for r in ac if r["cache_built_this_run"]), None)
        warm = [r for r in ac if not r["cache_built_this_run"]]
        warm_wall = sum(r["wall_s"] for r in warm) / len(warm) if warm else 0
        warm_fetch = sum(r["phase_fetch_ms"] for r in warm) / len(warm) if warm else 0
        if cold:
            print(
                f"  {repo:9} COLD wall={cold['wall_s']:.2f}s fetch={cold['phase_fetch_ms']:.0f}ms"
                f"  |  WARM avg wall={warm_wall:.2f}s fetch={warm_fetch:.0f}ms"
                f"  |  net={fmt_bytes(cold['bytes_proxy_out'])}"
            )
    # Method comparison on warm steady-state network
    print("\nSteady-state network per method (avg of all runs):")
    for repo in report["repos"]:
        line = f"  {repo:9}"
        for method in METHODS:
            ms = [r for r in runs if r["repo"] == repo and r["method"] == method and not r["error"]]
            if ms:
                avg = sum(r["bytes_proxy_out"] for r in ms) / len(ms)
                line += f"  {method}={fmt_bytes(avg):>10}"
        print(line)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repos", nargs="*", default=ALL_REPOS)
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--pct", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=1000)
    args = p.parse_args(argv)
    asyncio.run(run(args.repos, args.iterations, args.pct, args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
