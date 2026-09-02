# Experiment results

> All figures on this page are recomputed from the committed raw data in this
> directory (`exp1_cold_warm.json`, `exp2_taint.json`, `exp3_hook_update.json`).
> Warm = mean of iterations 2-5; bytes are proxy-measured outbound bytes.

Real agentic task (edit 2 % of source files, add `!` to a comment, commit),
measured end-to-end through a byte-counting proxy across a **15-repo polyglot
fleet** — chosen to stress unusual git patterns (submodules, empty files, LFS
pointers, huge binary notebooks, thousands of tiny files, non-Python languages).

## Experiment 1 — Cold vs warm cache (steady-state network per run)

| Repo | lang / note | files | naive | blobless | blobless+batch | **agentgitsmart** | vs naive |
|------|-------------|------:|------:|---------:|---------------:|---------------:|---------:|
| anthropic-cookbook | notebooks + images | 574 | 153 MiB | 45 KiB | 45 KiB | **16 KiB** | 9,562× |
| ohmyzsh | 1000s of tiny shell files | 1,091 | 3 MiB | 60 KiB | 61 KiB | **5 KiB** | 645× |
| prettier | JS, weird test fixtures | 9,373 | 6 MiB | 548 KiB | 549 KiB | **42 KiB** | 154× |
| bat | Rust, submodule assets | 1,004 | 2 MiB | 60 KiB | 63 KiB | **15 KiB** | 146× |
| cpython | C / Python | 5,801 | 43 MiB | 562 KiB | 562 KiB | **349 KiB** | 126× |
| django | Python, empty __init__ | 7,070 | 12 MiB | 491 KiB | 492 KiB | **138 KiB** | 89× |
| go | Go, 15k files | 15,596 | 36 MiB | 1044 KiB | 1044 KiB | **485 KiB** | 76× |
| jq | C, submodule (oniguruma) | 429 | 1 MiB | 32 KiB | 32 KiB | **17 KiB** | 76× |
| git | C, submodule gitlink | 4,765 | 12 MiB | 364 KiB | 365 KiB | **210 KiB** | 60× |
| redis | C, branch `unstable` | 1,818 | 5 MiB | 183 KiB | 183 KiB | **122 KiB** | 41× |
| anthropic-sdk-python | stainless-generated | 1,189 | 1 MiB | 77 KiB | 77 KiB | **32 KiB** | 34× |
| git-lfs | Go, LFS pointers | 650 | 878 KiB | 57 KiB | 58 KiB | **28 KiB** | 31× |
| ripgrep | Rust | 222 | 650 KiB | 49 KiB | 49 KiB | **39 KiB** | 17× |
| fd | Rust | 57 | 143 KiB | 11 KiB | 12 KiB | **9 KiB** | 17× |
| codex | OpenAI agent (Rust/TS) | 5,259 | 10 MiB | 877 KiB | 877 KiB | **659 KiB** | 16× |

**agentgitsmart is the bandwidth winner on all 15 repos** (16×–9,562× less than
naive), while also delivering **full history** (blobless is `--depth=1`, no
history) in a **single round-trip**. The first visit pays a one-time, server-
side lazy build; every later agent on that commit reuses it.

The extreme case (anthropic-cookbook, 153 MiB → 16 KiB) is a repo full of
Jupyter notebooks with embedded image/output bytes: naive drags down all of it,
agentgitsmart fetches only the handful of files the agent actually touches.

**Cold (first visit) vs warm (subsequent), agentgitsmart wall time:**

| Repo | COLD | WARM avg | one-time build cost |
|------|-----:|---------:|--------------------:|
| cpython | 27.53s | 12.44s | +15.09s |
| django  | 21.76s | 11.19s | +10.56s |
| go      | 38.72s | 14.47s | +24.24s |
| redis   |  4.80s |  1.58s |  +3.23s |

The first visit pays a one-time, server-side cache-build cost (lazy
generation: manifest + ctags symbol index). Every later agent on that commit
reuses it — the warm path is measurably faster, and the cache survives for all
subsequent agents. naive/blobless have no cache, so they are a flat baseline
with no cold/warm distinction.

## Experiment 2 — Can a non-aware agent taint the cache?

**Verdict: PRISTINE on all 4 repos** (codex, prettier, git-lfs, jq).

After an aware agent built and used the cache, we ran naive + blobless
(non-agentgitsmart-aware) agents against the same repo, then returned to the aware
agent. The cache-ref fingerprints were byte-identical before and after, and the
aware agent's warm path was unchanged (no rebuild). The cache is keyed by
immutable commit OID and lives in side refs that agents only *read*, so
read-only non-aware agents cannot corrupt it.

## Experiment 3 — Human push updates the cache via the hook

**Verdict: PASS.**

On a repo with the agentgitsmart `post-receive` hook installed, a human (no
agentgitsmart awareness) clones, edits, commits, and pushes. The hook fires
server-side and writes `refs/agent-git-smart/<new-oid>` automatically; the new
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
   a limitation of the *test harness*, not agentgitsmart. Fixed: the agent is now
   language-aware (`.rs`/`.go`/`.js`/`.c`/`.lua`/… with `//`, `--`, `#`
   comment styles), so it exercises agentgitsmart across every project.

## The fleet (15 repos, chosen to be "weird")

cpython, django, go, git, redis, **codex** (OpenAI agent CLI),
**anthropic-sdk-python**, **anthropic-cookbook**, jq, bat, ripgrep, prettier,
ohmyzsh, git-lfs, fd. Adding another is just
`git clone --mirror <url> benchmark/repos/<name>.git && python -m experiments.prep` —
the harness discovers repos automatically.
