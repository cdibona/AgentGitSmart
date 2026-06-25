# Experiment results

Real agentic task (edit 2 % of source files, add `!` to a comment, commit),
measured end-to-end through a byte-counting proxy across a **15-repo polyglot
fleet** — chosen to stress unusual git patterns (submodules, empty files, LFS
pointers, huge binary notebooks, thousands of tiny files, non-Python languages).

## Experiment 1 — Cold vs warm cache (steady-state network per run)

| Repo | lang / note | files | naive | blobless | **agentcache** | vs naive |
|------|-------------|------:|------:|---------:|---------------:|---------:|
| anthropic-cookbook | notebooks + images | 574 | 153 MiB | 44 KiB | **16 KiB** | **9,979×** |
| ohmyzsh | 1000s of tiny shell files | 1,091 | 3 MiB | 61 KiB | **6 KiB** | 570× |
| bat | Rust, submodule assets | 1,004 | 2 MiB | 60 KiB | **14 KiB** | 150× |
| prettier | JS, weird test fixtures | 9,373 | 6 MiB | 549 KiB | **44 KiB** | 150× |
| jq | C, submodule (oniguruma) | 429 | 1 MiB | 24 KiB | **9 KiB** | 140× |
| cpython | C / Python | 5,801 | 43 MiB | 586 KiB | **372 KiB** | 118× |
| go | Go, 15k files | 15,596 | 36 MiB | 1014 KiB | **462 KiB** | 80× |
| django | Python, empty __init__ | 7,070 | 12 MiB | 515 KiB | **162 KiB** | 76× |
| git | C, submodule gitlink | 4,765 | 12 MiB | 351 KiB | **197 KiB** | 65× |
| redis | C, branch `unstable` | 1,818 | 5 MiB | 156 KiB | **95 KiB** | 52× |
| anthropic-sdk-python | stainless-generated | 1,189 | 1 MiB | 79 KiB | **34 KiB** | 32× |
| git-lfs | Go, LFS pointers | 650 | 878 KiB | 56 KiB | **27 KiB** | 32× |
| fd | Rust | 57 | 143 KiB | 10 KiB | **7 KiB** | 20× |
| codex | OpenAI agent (Rust/TS) | 5,259 | 10 MiB | 845 KiB | **628 KiB** | 17× |
| ripgrep | Rust | 222 | 650 KiB | 48 KiB | **38 KiB** | 17× |

**agentcache is the bandwidth winner on all 15 repos** (17×–9,979× less than
naive), while also delivering **full history** (blobless is `--depth=1`, no
history) in a **single round-trip**. The first visit pays a one-time, server-
side lazy build; every later agent on that commit reuses it.

The extreme case (anthropic-cookbook, 153 MiB → 16 KiB) is a repo full of
Jupyter notebooks with embedded image/output bytes: naive drags down all of it,
agentcache fetches only the handful of files the agent actually touches.

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
3. **Python-only test agent** — the synthetic agent only recognised `.py`
   files and `#` comments, so it errored ("No source files found") on the
   polyglot fleet (Rust `fd`/`ripgrep`, Go `git-lfs`, JS `prettier`). This was
   a limitation of the *test harness*, not agentcache. Fixed: the agent is now
   language-aware (`.rs`/`.go`/`.js`/`.c`/`.lua`/… with `//`, `--`, `#`
   comment styles), so it exercises agentcache across every project.

## The fleet (15 repos, chosen to be "weird")

cpython, django, go, git, redis, **codex** (OpenAI agent CLI),
**anthropic-sdk-python**, **anthropic-cookbook**, jq, bat, ripgrep, prettier,
ohmyzsh, git-lfs, fd. Adding another is just
`git clone --mirror <url> benchmark/repos/<name>.git && python -m experiments.prep` —
the harness discovers repos automatically.
