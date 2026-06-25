# Experiment results

Real agentic task (edit 2 % of source files, add `!` to a comment, commit),
5 iterations per method, measured end-to-end through a byte-counting proxy on
the five collected CPython-sized projects.

## Experiment 1 — Cold vs warm cache

**Steady-state network per agent run (bytes received from the git server):**

| Repo | files | naive | blobless | **agentcache** | agentcache vs naive |
|------|------:|------:|---------:|---------------:|--------------------:|
| cpython | 5,801 | 43 MiB | 454 KiB | **247 KiB** | 178× less |
| django  | 7,070 | 12 MiB | 496 KiB | **143 KiB** | 86× less |
| go      | 15,596 | 36 MiB | 542 KiB | **42 KiB** | 878× less |
| git     | 4,765 | 12 MiB | 283 KiB | **138 KiB** | 89× less |
| redis   | 1,818 | 5 MiB | 102 KiB | **44 KiB** | 116× less |

agentcache is the bandwidth winner on every repo, while also delivering **full
history** (blobless is `--depth=1`, no history) in a **single round-trip**.

**Cold (first visit) vs warm (subsequent), agentcache wall time:**

| Repo | COLD | WARM avg | one-time build cost |
|------|-----:|---------:|--------------------:|
| cpython | 5.49s | 4.36s | +1.13s |
| django  | 6.62s | 5.91s | +0.72s |
| go      | 4.42s | 2.86s | +1.56s |
| redis   | 0.74s | 0.48s | +0.26s |

The first visit pays a one-time, server-side cache-build cost (lazy
generation: manifest + ctags symbol index). Every later agent on that commit
reuses it — the warm path is measurably faster, and the cache survives for all
subsequent agents. naive/blobless have no cache, so they are a flat baseline
with no cold/warm distinction.

## Experiment 2 — Can a non-aware agent taint the cache?

**Verdict: PRISTINE on all 5 repos.**

After an aware agent built and used the cache, we ran naive + blobless
(non-agentcache-aware) agents against the same repo, then returned to the aware
agent. The cache-ref fingerprints were byte-identical before and after, and the
aware agent's warm path was unchanged (no rebuild). The cache is keyed by
immutable commit OID and lives in side refs that agents only *read*, so
read-only non-aware agents cannot corrupt it.

## Experiment 3 — Human push updates the cache via the hook

**Verdict: PASS.**

On a repo with the agentcache `post-receive` hook installed, a human (no
agentcache awareness) clones, edits, commits, and pushes. The hook fires
server-side and writes `refs/agent-cache/<new-oid>` automatically; the new
commit's manifest reflects the human's change. A second push produces a second
cache ref. Human activity keeps the cache current with zero agent involvement.

## Bugs the experiments surfaced (and fixed)

1. **django batch-fetch failure** — clearing `remote.origin.partialclonefilter`
   broke blob materialisation on some repos, and `git fetch <blob-oid>` exits
   non-zero even when blobs arrive. Fixed: drop the override, treat
   "objects present locally" as success, promisor-fetch any stragglers, and
   filter the intrinsic empty-blob OID.
2. **git submodule crash** — `git.git` has a gitlink (`sha1collisiondetection`)
   whose commit isn't in the object store; the manifest builder crashed on it.
   Fixed: detect gitlinks by mode (160000) and never dereference them.
