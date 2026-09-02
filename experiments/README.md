# agentgitsmart experiments

**→ [`RECENT.md`](RECENT.md) — a readable digest of the most recent harness
runs** (what each run did, per-repo win-vs-naive tables, the per-human-commit
cache-rebuild load, and the hook-vs-GitHub-Action warm comparison). Regenerate
with `python scripts/render_experiment_report.py`; raw JSON lives under
[`results/harness/`](results/harness/). Note that the regenerator reads
`testharness/data/experiments/`, which is gitignored — on a fresh clone it
prints "No complete experiments found" and leaves the committed `RECENT.md`
alone. Run the harness first to produce new data.

Four studies that compare the repo-access methods — **naive**, **blobless**,
**blobless+batch**, **agentgitsmart** — across the collected repo fleet
(15 repos in the committed results: cpython, django, go, git, redis, codex,
anthropic-sdk-python, anthropic-cookbook, jq, bat, ripgrep, prettier, ohmyzsh,
git-lfs, fd), and probe the agentgitsmart cache lifecycle. Each study takes
`--repos` to narrow the fleet.

Each agent run performs a real agentic task: discover all source files,
select a deterministic 2 %, add `!` to one comment per file, and commit
locally — measured end-to-end through a byte-counting proxy.

## Prerequisites

```bash
# The repos must be mirrored under benchmark/repos/<name>.git
# and blobless bootstrap bundles under benchmark/bundles/<name>.git-<branch>.bundle
# (both are produced by the setup steps; bundles let agentgitsmart seed history
#  from a local/CDN file instead of the git server).
```

## Experiment 1 — Cold vs warm cache

```bash
python -m experiments.exp1_cold_warm                 # whole fleet, 5 iterations
python -m experiments.exp1_cold_warm --repos redis --iterations 3
```

For each repo it **erases any existing cache first** (starts from nothing),
then runs each method N times. The first agentgitsmart visit is the *cold*
build (it triggers lazy generation server-side); subsequent visits are
*warm* (the cache already exists). naive/blobless have no cache, so they
form a flat baseline.

→ `results/exp1_cold_warm.json`

## Experiment 2 — Can a non-aware agent taint the cache?

```bash
python -m experiments.exp2_taint --repos redis git django
```

Builds + uses the cache with an aware agent, fingerprints the cache refs,
then runs **non-agentgitsmart-aware** agents (naive + blobless) against the
same repo, and re-checks. Because the cache is keyed by immutable commit
OID and lives in side refs the agents only *read*, read-only non-aware
agents leave it **pristine** — verified by comparing ref fingerprints
before/after and confirming the aware agent's warm path is unchanged.

→ `results/exp2_taint.json`

## Experiment 3 — A human push updates the cache via the git hook

```bash
python -m experiments.exp3_hook_update
```

On a throwaway bare repo with the agentgitsmart `post-receive` hook installed,
a human (no agentgitsmart awareness) clones, edits, commits, and pushes. The
hook fires on the server and writes `refs/agent-git-smart/<new-oid>`
automatically. Verifies the new commit's cache exists, its manifest
reflects the human's change, and a second push produces a second cache ref.

→ `results/exp3_hook_update.json`

## Experiment 4 — Does batching save bytes, or only round-trips?

```bash
python -m experiments.exp4_ref_ads
```

Tests whether N lazy per-file fetches each re-pay a *ref advertisement* (which
would make batching a bandwidth win) across git protocol **v2 and v0**, with up
to 5,000 synthetic `refs/pull/*` refs injected. Finding: batching saved **<5% of
bytes in every cell** while always cutting round-trips — so blobless+batch is a
**latency** win, not a bandwidth one.

→ `results/exp4_ref_ads.json`

## How the pieces fit

| Mechanism | Built by | Keyed by | Lifecycle |
|-----------|----------|----------|-----------|
| Cache (manifest + symbols) | post-receive hook **or** lazy gen on first request | commit OID | immutable per commit; new commit → new cache; symbol index uses delta re-indexing (changed files only per push); `meta.json` includes a `generation` block with `mode`, `files_reindexed`, `files_carried_forward`, and `fallback_reason` |
| Bundle (history seed) | post-receive hook (or pre-built) | branch | refreshed on push |
| Erase | `agentgitsmart-uninstall` | — | removes all side refs + gc |

The cache cannot be corrupted by read-only agents; it is only ever *added
to* (lazy gen / hook) or *removed* (uninstall). Human pushes keep it
current through the hook with zero agent involvement.
