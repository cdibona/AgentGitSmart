"""Experiment 4 — Does batching save BANDWIDTH, or just round-trips?

Motivation
----------
The ``blobless_batch`` arm (AgentGitSmartBlobless) collapses N lazy per-file
promisor fetches into ONE batched ``git fetch``.  The intuition (see the
``--no-tags`` note in ``testharness/real_agent.py``) is that each fetch repeats a
*ref advertisement*, so on a busy remote with tens of thousands of ``refs/pull/*``
refs, N fetches would each re-pay that advertisement — and batching would save it.

This study tests that intuition HONESTLY, with real proxy-measured bytes, across
the two axes that would drive it:

  * protocol version — ``v2`` (modern default; ``ls-refs`` filters the
    advertisement) vs ``v0`` (legacy; full advertisement each negotiation)
  * ref density — the repo as-is vs the same repo with N thousand synthetic
    ``refs/sim/pr/*`` refs injected (a stand-in for a busy fork's PR refs)

For each cell we run the ``blobless`` (N lazy fetches) and ``blobless_batch``
(1 batched fetch) arms and compare bytes AND round-trips.

Synthetic refs are injected into the bare mirror before the run and deleted
afterwards (``refs/sim/*`` only — never touches real refs).  The protocol version
is forced per-cell via a temporary ``GIT_CONFIG_GLOBAL``.

Output: experiments/results/exp4_ref_ads.json + a printed table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .harness import ExperimentHarness, fmt_bytes

RESULTS = Path(__file__).resolve().parent / "results"
SIM_REF_PREFIX = "refs/sim/pr"


# ---------------------------------------------------------------------------
# Synthetic ref injection (real refs → real advertisement bytes on the wire)
# ---------------------------------------------------------------------------


def _git_dir(repos_dir: str, repo: str) -> str:
    return str(Path(repos_dir) / f"{repo}.git")


def _head_oid(git_dir: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", git_dir, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def inject_sim_refs(git_dir: str, n: int) -> int:
    """Create *n* synthetic ``refs/sim/pr/<i>`` refs pointing at HEAD.

    Returns the number of refs that exist under the sim prefix afterwards.
    Uses a single ``git update-ref --stdin`` batch, so it is fast even for
    thousands of refs.
    """
    if n <= 0:
        return 0
    oid = _head_oid(git_dir)
    payload = "".join(f"create {SIM_REF_PREFIX}/{i} {oid}\n" for i in range(n))
    subprocess.run(
        ["git", "--git-dir", git_dir, "update-ref", "--stdin"],
        input=payload, capture_output=True, text=True, check=True,
    )
    return count_sim_refs(git_dir)


def count_sim_refs(git_dir: str) -> int:
    r = subprocess.run(
        ["git", "--git-dir", git_dir, "for-each-ref", "--format=%(refname)",
         SIM_REF_PREFIX.rsplit("/", 1)[0] + "/"],
        capture_output=True, text=True,
    )
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def clear_sim_refs(git_dir: str) -> int:
    """Delete every ``refs/sim/*`` ref (never touches real refs). Returns count removed."""
    base = SIM_REF_PREFIX.rsplit("/", 1)[0] + "/"
    r = subprocess.run(
        ["git", "--git-dir", git_dir, "for-each-ref", "--format=%(refname)", base],
        capture_output=True, text=True,
    )
    refs = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not refs:
        return 0
    payload = "".join(f"delete {ref}\n" for ref in refs)
    subprocess.run(
        ["git", "--git-dir", git_dir, "update-ref", "--stdin"],
        input=payload, capture_output=True, text=True, check=True,
    )
    return len(refs)


# ---------------------------------------------------------------------------
# Study
# ---------------------------------------------------------------------------


async def _measure_cell(
    h: ExperimentHarness, repo: str, protocol: int, pct: float, seed: int
) -> dict:
    """Run blobless + blobless_batch for one (repo, protocol) cell; return bytes/rt."""
    cell: dict = {"protocol": protocol}
    for method in ("blobless", "blobless_batch"):
        h.clear_cache(repo, gc=False)
        res = await h.run_agent(repo, method, iteration=2, seed=seed, pct=pct)
        cell[method] = {
            "bytes_out": res.bytes_proxy_out,
            "roundtrips": res.fetch_roundtrips,
            "files_selected": res.files_selected,
            "error": res.error,
        }
    bl = cell["blobless"]["bytes_out"]
    blb = cell["blobless_batch"]["bytes_out"]
    cell["batch_saves_bytes"] = bl - blb
    cell["batch_saves_pct"] = round((bl - blb) / bl * 100, 1) if bl else None
    cell["batch_saves_roundtrips"] = (
        cell["blobless"]["roundtrips"] - cell["blobless_batch"]["roundtrips"]
    )
    return cell


async def run(repos, ref_densities, protocols, pct, seed) -> dict:
    h = ExperimentHarness(git_port=9580, proxy_port=9581, svc_port=8798)
    await h.start()
    prev_global = os.environ.get("GIT_CONFIG_GLOBAL")
    results: list[dict] = []
    try:
        for repo in repos:
            git_dir = _git_dir(h.repos_dir, repo)
            for density in ref_densities:
                clear_sim_refs(git_dir)
                injected = inject_sim_refs(git_dir, density)
                total_refs = _total_refs(git_dir)
                try:
                    for protocol in protocols:
                        cfgpath = tempfile.mktemp(prefix=f"gitcfg_v{protocol}_")
                        Path(cfgpath).write_text(
                            f"[protocol]\n\tversion = {protocol}\n"
                        )
                        os.environ["GIT_CONFIG_GLOBAL"] = cfgpath
                        try:
                            cell = await _measure_cell(h, repo, protocol, pct, seed)
                        finally:
                            try:
                                os.remove(cfgpath)
                            except OSError:
                                pass
                        cell.update(
                            repo=repo,
                            sim_refs_injected=injected,
                            total_refs=total_refs,
                        )
                        results.append(cell)
                        _print_cell(cell)
                finally:
                    removed = clear_sim_refs(git_dir)
                    print(f"    (cleaned up {removed} sim ref(s) on {repo})")
    finally:
        if prev_global is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = prev_global
        await h.stop()

    report = {
        "experiment": "ref_advertisement",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pct": pct,
        "repos": repos,
        "ref_densities": ref_densities,
        "protocols": protocols,
        "cells": results,
        "finding": _finding(results),
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "exp4_ref_ads.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nFINDING: {report['finding']}")
    print(f"Wrote {out}")
    return report


def _total_refs(git_dir: str) -> int:
    r = subprocess.run(
        ["git", "--git-dir", git_dir, "for-each-ref", "--format=%(refname)"],
        capture_output=True, text=True,
    )
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def _print_cell(cell: dict) -> None:
    bl = cell["blobless"]
    blb = cell["blobless_batch"]
    print(
        f"  {cell['repo']:10} refs={cell['total_refs']:>6} proto=v{cell['protocol']}  "
        f"blobless={fmt_bytes(bl['bytes_out']):>10}/{bl['roundtrips']}rt  "
        f"batch={fmt_bytes(blb['bytes_out']):>10}/{blb['roundtrips']}rt  "
        f"Δbytes={fmt_bytes(cell['batch_saves_bytes']):>10} "
        f"({cell['batch_saves_pct']}%)  Δrt=-{cell['batch_saves_roundtrips']}"
    )


def _finding(cells: list[dict]) -> str:
    """One-line honest summary: does batching ever save meaningful bytes?"""
    if not cells:
        return "no data"
    max_byte_saving_pct = max(
        (c["batch_saves_pct"] or 0) for c in cells
    )
    always_saves_rt = all(c["batch_saves_roundtrips"] > 0 for c in cells)
    if max_byte_saving_pct < 5:
        return (
            "Across every protocol/ref-density cell, batching saved <5% of bytes "
            "(often 0 or slightly negative) while always cutting round-trips. "
            "AgentGitSmartBlobless is a LATENCY optimisation, not a bandwidth one: "
            "real git charges lazy and batched fetching the same bytes; injecting "
            "refs inflates the CLONE cost, which BOTH arms pay equally."
            if always_saves_rt
            else "batching saved <5% of bytes across all cells (bandwidth-neutral)."
        )
    return (
        f"batching saved up to {max_byte_saving_pct}% of bytes in some cell — "
        "inspect which protocol/ref-density made the difference."
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repos", nargs="*", default=["bat", "ripgrep"])
    p.add_argument(
        "--ref-densities", nargs="*", type=int, default=[0, 5000],
        help="Synthetic refs to inject per cell (0 = repo as-is).",
    )
    p.add_argument(
        "--protocols", nargs="*", type=int, default=[2, 0],
        help="git wire protocol versions to test.",
    )
    p.add_argument("--pct", type=float, default=10.0,
                   help="%% of source files edited (more files → more lazy fetches).")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args(argv)
    asyncio.run(
        run(args.repos, args.ref_densities, args.protocols, args.pct, args.seed)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
