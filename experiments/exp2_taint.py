"""Experiment 2 — Can a non-agentcache-aware agent taint the cache?

Scenario per repo:
  1. Erase cache, then run an agentcache-aware agent → builds + uses the cache.
     Snapshot the cache state (ref names + their object ids).
  2. Run NON-aware agents (naive, then blobless) against the same repo+commit.
     These clone into their own disposable workspaces and never push, so the
     hypothesis is: they cannot touch the server's cache.
  3. Snapshot the cache state again, and run the agentcache-aware agent once
     more.  Compare:
        - did any cache ref change? (taint check)
        - did the aware agent's run flow change? (still warm, same bytes?)

What it demonstrates: the cache is keyed by immutable commit OID and lives in
side refs the agents only READ, so read-only non-aware agents leave it
pristine.  The aware agent's warm path is unchanged before vs after.

Output: experiments/results/exp2_taint.json + printed verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import pygit2

from .harness import ExperimentHarness, fmt_bytes, REF_PREFIX
from agentcache import uninstall as uninstall_mod

RESULTS = Path(__file__).resolve().parent / "results"


def _cache_fingerprint(repo_dir: str) -> dict:
    """Map every agent-cache ref -> the object id it points at."""
    r = pygit2.Repository(repo_dir)
    fp = {}
    for name in uninstall_mod.find_cache_refs(r, REF_PREFIX):
        fp[name] = str(r.references[name].target)
    return fp


async def run_one(h: ExperimentHarness, repo: str, seed: int, pct: float) -> dict:
    h.clear_cache(repo, gc=False)
    repo_dir = h.repo_dir(repo)
    print(f"\n=== {repo} ===")

    # 1. aware agent builds + uses the cache
    build = await h.run_agent(repo, "agentcache", iteration=1, seed=seed, pct=pct)
    fp_before = _cache_fingerprint(repo_dir)
    print(
        f"  [1] agentcache build: built={build.cache_built_this_run} "
        f"net={fmt_bytes(build.bytes_proxy_out)} refs={len(fp_before)}"
    )

    # warm baseline BEFORE the non-aware runs
    warm_before = await h.run_agent(
        repo, "agentcache", iteration=2, seed=seed + 1, pct=pct
    )
    print(
        f"  [2] agentcache warm (before): net={fmt_bytes(warm_before.bytes_proxy_out)} "
        f"wall={warm_before.wall_s:.2f}s fetch={warm_before.phase_fetch_ms:.0f}ms"
    )

    # 3. non-aware agents hammer the same repo+commit
    naive = await h.run_agent(repo, "naive", iteration=3, seed=seed + 2, pct=pct)
    blobless = await h.run_agent(repo, "blobless", iteration=4, seed=seed + 3, pct=pct)
    print(
        f"  [3] non-aware naive:    net={fmt_bytes(naive.bytes_proxy_out)} mod={naive.files_modified}"
    )
    print(
        f"      non-aware blobless: net={fmt_bytes(blobless.bytes_proxy_out)} "
        f"roundtrips={blobless.fetch_roundtrips} mod={blobless.files_modified}"
    )

    # 4. cache state after non-aware runs + aware agent again
    fp_after = _cache_fingerprint(repo_dir)
    warm_after = await h.run_agent(
        repo, "agentcache", iteration=5, seed=seed + 1, pct=pct
    )

    tainted = fp_before != fp_after
    flow_changed = warm_after.cache_built_this_run  # would mean cache vanished/rebuilt
    print(
        f"  [4] cache tainted? {tainted}  (refs before={len(fp_before)} after={len(fp_after)})"
    )
    print(
        f"      agentcache warm (after):  net={fmt_bytes(warm_after.bytes_proxy_out)} "
        f"wall={warm_after.wall_s:.2f}s fetch={warm_after.phase_fetch_ms:.0f}ms "
        f"rebuilt={flow_changed}"
    )

    verdict = "PRISTINE" if (not tainted and not flow_changed) else "TAINTED"
    print(f"  ==> {repo}: cache {verdict} after non-aware agents")

    return {
        "repo": repo,
        "fingerprint_before": fp_before,
        "fingerprint_after": fp_after,
        "tainted": tainted,
        "aware_rebuilt_after": flow_changed,
        "verdict": verdict,
        "runs": {
            "build": build.to_dict(),
            "warm_before": warm_before.to_dict(),
            "naive": naive.to_dict(),
            "blobless": blobless.to_dict(),
            "warm_after": warm_after.to_dict(),
        },
    }


async def run(repos, pct: float, base_seed: int) -> dict:
    h = ExperimentHarness()
    await h.start()
    results = []
    try:
        for i, repo in enumerate(repos):
            results.append(await run_one(h, repo, base_seed + i * 10, pct))
    finally:
        await h.stop()

    report = {
        "experiment": "taint",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pct": pct,
        "results": results,
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "exp2_taint.json"
    out.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 72)
    print("VERDICT — did non-aware agents taint the cache?")
    print("=" * 72)
    for r in results:
        print(f"  {r['repo']:9} {r['verdict']}")
    all_pristine = all(r["verdict"] == "PRISTINE" for r in results)
    print(f"\nAll repos pristine: {all_pristine}")
    print(f"Wrote {out}")
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repos", nargs="*", default=["redis", "git", "django"])
    p.add_argument("--pct", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=2000)
    args = p.parse_args(argv)
    asyncio.run(run(args.repos, args.pct, args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
