# agentcache experiments

Three studies that compare the three repo-access methods — **naive**,
**blobless**, **agentcache** — across the five collected CPython-sized
projects (`cpython`, `django`, `go`, `git`, `redis`), and probe the
agentcache cache lifecycle.

Each agent run performs a real agentic task: discover all source files,
select a deterministic 2 %, add `!` to one comment per file, and commit
locally — measured end-to-end through a byte-counting proxy.

## Prerequisites

```bash
# The five repos must be mirrored under benchmark/repos/<name>.git
# and blobless bootstrap bundles under benchmark/bundles/<name>.git-<branch>.bundle
# (both are produced by the setup steps; bundles let agentcache seed history
#  from a local/CDN file instead of the git server).
```

## Experiment 1 — Cold vs warm cache

```bash
python -m experiments.exp1_cold_warm                 # all 5 repos, 5 iterations
python -m experiments.exp1_cold_warm --repos redis --iterations 3
```

For each repo it **erases any existing cache first** (starts from nothing),
then runs each method N times. The first agentcache visit is the *cold*
build (it triggers lazy generation server-side); subsequent visits are
*warm* (the cache already exists). naive/blobless have no cache, so they
form a flat baseline.

→ `results/exp1_cold_warm.json`

## Experiment 2 — Can a non-aware agent taint the cache?

```bash
python -m experiments.exp2_taint --repos redis git django
```

Builds + uses the cache with an aware agent, fingerprints the cache refs,
then runs **non-agentcache-aware** agents (naive + blobless) against the
same repo, and re-checks. Because the cache is keyed by immutable commit
OID and lives in side refs the agents only *read*, read-only non-aware
agents leave it **pristine** — verified by comparing ref fingerprints
before/after and confirming the aware agent's warm path is unchanged.

→ `results/exp2_taint.json`

## Experiment 3 — A human push updates the cache via the git hook

```bash
python -m experiments.exp3_hook_update
```

On a throwaway bare repo with the agentcache `post-receive` hook installed,
a human (no agentcache awareness) clones, edits, commits, and pushes. The
hook fires on the server and writes `refs/agent-cache/<new-oid>`
automatically. Verifies the new commit's cache exists, its manifest
reflects the human's change, and a second push produces a second cache ref.

→ `results/exp3_hook_update.json`

## How the pieces fit

| Mechanism | Built by | Keyed by | Lifecycle |
|-----------|----------|----------|-----------|
| Cache (manifest + symbols) | post-receive hook **or** lazy gen on first request | commit OID | immutable per commit; new commit → new cache |
| Bundle (history seed) | post-receive hook (or pre-built) | branch | refreshed on push |
| Erase | `agentcache-uninstall` | — | removes all side refs + gc |

The cache cannot be corrupted by read-only agents; it is only ever *added
to* (lazy gen / hook) or *removed* (uninstall). Human pushes keep it
current through the hook with zero agent involvement.
