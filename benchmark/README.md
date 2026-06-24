# agentcache Benchmark

Compares three agent cold-start approaches side-by-side.
Everything runs against a **local** bare git repo — nothing reaches a
public host.

---

## What is being tested

An AI coding agent receives a task: "edit these N files."
How quickly can it go from **zero** to **content in hand**?

| Approach | What git does | Files materialised |
|----------|--------------|-------------------|
| **naive** | `git clone --depth=1` — all blobs | Every tracked file |
| **blobless** | `git clone --filter=blob:none` then `checkout -- <paths>` | Only target files, but one lazy fetch per blob |
| **agentcache** | blobless clone → POST `/resolve` → ONE batched `git fetch <oids>` | Only target blobs, one round-trip |

The difference is negligible on a tiny repo.  On CPython (~60 k files,
~300 MB shallow clone) the contrast is dramatic.

---

## Quick start — smoke test

No setup required.  Spins up an in-process fixture repo, generates the
cache, starts the service, and benchmarks all three approaches:

```bash
cd /path/to/PackCache
.venv/bin/python -m benchmark.run --smoke
```

Expected output:

```
=== agentcache smoke benchmark ===
Creating fixture repo...
Generating agentcache artifacts for a1b2c3d4...
Starting agentcache service...
Benchmarking (3 run(s) each, target: ['src/app.py'])...
  [naive] running 3 time(s)......
  [blobless] running 3 time(s)......
  [agentcache] running 3 time(s)......

=== Results ===

Approach                                    Wall time  Recv bytes  Objects recv  Disk (clone dir)  Files on disk  Note
-------------------------------------------  ---------  ----------  ------------  ----------------  -------------  ---...
naive (depth=1 clone)                        0.045s     3.0 KiB     5             12.0 KiB          3              every tracked file materialized...
blobless (filter=blob:none + sparse checkout) 0.042s    1.0 KiB     4             8.0 KiB           2              1 lazy fetch(es) triggered...
agentcache (blobless → resolve → targeted)   0.038s     1.0 KiB     1             6.0 KiB           0              one packfile; 1 path(s) → 1 blob(s)...
```

> On a tiny fixture repo the numbers converge — the benchmark is
> showing you *correctness* (same result, different paths).  The
> differences explode on a large repo.

---

## Full benchmark against a large local repo (e.g. CPython)

### Step 1 — get CPython on disk

You need a local copy of the CPython source.  Obtain it however you
like (existing clone, a backup, an offline mirror — no specific host is
required).

```bash
# Example: you already have it at ~/src/cpython
ls ~/src/cpython/.git   # should exist
```

### Step 2 — set up the benchmark repo

```bash
bash benchmark/setup_repo.sh \
    --source ~/src/cpython \
    benchmark/repos/cpython.git
```

This will:
1. Mirror your local CPython into `benchmark/repos/cpython.git`
2. Configure `uploadpack.allowFilter` and `allowAnySHA1InWant`
3. Generate agentcache artifacts for every branch tip

Takes a few seconds on a fast SSD (mirror is local copy).

### Step 3 — start the agentcache service

In a separate terminal (or background it):

```bash
AGENTCACHE_REPO_DIR=benchmark/repos/cpython.git \
    .venv/bin/python -m agentcache.service
```

### Step 4 — run the benchmark

```bash
.venv/bin/python -m benchmark.run \
    --repo benchmark/repos/cpython.git \
    --branch main \
    --paths Lib/asyncio/tasks.py Lib/ast.py \
    --runs 3
```

Or let the benchmark start the service itself:

```bash
.venv/bin/python -m benchmark.run \
    --repo benchmark/repos/cpython.git \
    --branch main \
    --paths Lib/asyncio/tasks.py Lib/ast.py \
    --runs 3 \
    --start-service
```

### Optional — network-realistic test with `git daemon`

By default, `file://` URLs are used (local filesystem — no network
stack involved).  For a more realistic simulation of a remote agent:

```bash
# Start git daemon in the background
git daemon --reuseaddr \
    --base-path=benchmark/repos \
    --export-all \
    benchmark/repos/ &

# Use git:// URL instead of file://
.venv/bin/python -m benchmark.run \
    --repo git://localhost/cpython.git \
    --branch main \
    --paths Lib/asyncio/tasks.py \
    --runs 3
```

Note: pass the bare-repo path to `--repo` for the service; the daemon
and the benchmark runner both derive their URLs from it independently.

---

## What the numbers mean

| Metric | Meaning |
|--------|---------|
| **Wall time** | Total time from zero to "agent can read content" |
| **Recv bytes** | Bytes received from git (objects + pack overhead) |
| **Objects recv** | Git objects fetched (blobs, trees, commits) |
| **Disk (clone dir)** | Total size of the working clone on disk |
| **Files on disk** | Number of regular files materialised |

On CPython, you should expect roughly:

| Approach | Wall time | Disk | Files |
|----------|-----------|------|-------|
| naive (depth=1) | 30–60 s | ~400 MB | ~60 k |
| blobless | 5–10 s | ~20 MB | ~2 |
| agentcache | 3–6 s | ~15 MB | 0 (read by OID) |

*(Times depend heavily on storage speed and CPU.)*

---

## Save results to JSON

```bash
.venv/bin/python -m benchmark.run --smoke --output results/smoke.json
```
