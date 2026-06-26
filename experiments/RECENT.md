# Recent AgentCache experiments

These are real runs from the [test harness](../testharness/) measuring three git-fetch strategies — **naive** (full clone), **blobless** (`--filter=blob:none`), and **agentcache** (targeted blob fetch via the pre-built manifest + symbol cache). Each experiment runs multiple agent passes per repo: **pass 1 is COLD** (agentcache builds its cache from scratch on first access), and **later passes are WARM** (the cache already exists and only the requested blobs are fetched). Each interleaved human commit triggers a server-side cache update via the `post-receive` hook, keeping the next agent warm.

Regenerate with: `python scripts/render_experiment_report.py`

See also the headless studies: [SUMMARY.md](results/SUMMARY.md) · [exp1 (cold vs warm)](results/exp1_cold_warm.json) · [exp2 (cache taint)](results/exp2_taint.json) · [exp3 (hook update)](results/exp3_hook_update.json)

---

## a827b997 — 2026-06-26 22:15 UTC

Ran 2026-06-26 22:12 UTC → 2026-06-26 22:15 UTC (160s)

> Cache experiment over 4 repo(s) [anthropic-cookbook.git, anthropic-sdk-python.git, bat.git, codex.git]: 3 agent pass(es) per repo (pass 1 = COLD/builds the cache, later passes = WARM/steady state); methods compared: naive, blobless, agentcache; the agent edits 2.0% of source files each pass (seed 4244); 1 teammate (human) commit(s) interleaved between agent passes, cache warmed after each human commit via the 'hook' mechanism (hook = in-process server post-receive; action = github-action subprocess incl. blobless bundle; both = run and compare); the next agent stays warm; each agent pass runs in a fresh, disposable Docker container. Agent network cost is measured end-to-end through a byte-counting proxy; each human step records its own wall time and the cache-rebuild load it triggered (delta vs full, files reindexed/carried-forward, bytes materialized).

| Repo | files | naive (warm) | blobless (warm) | agentcache (warm) | agentcache cold | win vs naive |
|------|------:|-------------:|----------------:|------------------:|----------------:|-------------:|
| anthropic-cookbook.git | 574 | 153.3 MiB | 42.4 KiB | 15.4 KiB | 557.4 KiB | 10201.5× |
| anthropic-sdk-python.git | 1189 | 1.1 MiB | 77.0 KiB | 32.2 KiB | 858.9 KiB | 34.5× |
| bat.git | 1004 | 2.1 MiB | 53.9 KiB | 9.7 KiB | 2.3 MiB | 222.1× |
| codex.git | 5259 | 10.3 MiB | 875.8 KiB | 660.2 KiB | 19.6 MiB | 16.0× |

### Human steps

**anthropic-cookbook.git:**

  **Human #1** (5b4e59dca1) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=574, 21 B materialized, 7.33s

**anthropic-sdk-python.git:**

  **Human #1** (b983d462d8) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1189, 21 B materialized, 0.3762s

**bat.git:**

  **Human #1** (37bff21d7d) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1004, 21 B materialized, 0.4024s

**codex.git:**

  **Human #1** (0cc84f438c) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=5259, 21 B materialized, 4.6006s

---

## 263c0e79 — 2026-06-26 21:13 UTC

Ran 2026-06-26 21:13 UTC → 2026-06-26 21:13 UTC (13s)

> Cache experiment over 2 repo(s) [fd.git, ripgrep.git]: 2 agent pass(es) per repo (pass 1 = COLD/builds the cache, later passes = WARM/steady state); methods compared: naive, blobless, agentcache; the agent edits 2.0% of source files each pass (seed 1000); 1 teammate (human) commit(s) interleaved between agent passes, cache warmed after each human commit via the 'both' mechanism (hook = in-process server post-receive; action = github-action subprocess incl. blobless bundle; both = run and compare); the next agent stays warm; each agent pass runs in a fresh, disposable Docker container. Agent network cost is measured end-to-end through a byte-counting proxy; each human step records its own wall time and the cache-rebuild load it triggered (delta vs full, files reindexed/carried-forward, bytes materialized).

| Repo | files | naive (warm) | blobless (warm) | agentcache (warm) | agentcache cold | win vs naive |
|------|------:|-------------:|----------------:|------------------:|----------------:|-------------:|
| fd.git | 57 | 142.1 KiB | 12.9 KiB | 11.3 KiB | 1.1 MiB | 12.6× |
| ripgrep.git | 222 | 649.8 KiB | 51.5 KiB | 42.6 KiB | 1.5 MiB | 15.3× |

### Human steps

**fd.git:**

  **Human #1** (dde5c523bd) — warm_method=**both** (hook vs action)
  - hook: delta mode, reindexed=1, carried=57, 21 B materialized, 0.0724s
  - action: bundle 1.1 MiB, 0.3285s
  - **comparison**: hook 0.0724s vs action 0.3285s → **hook faster** (ratio 4.54×)

**ripgrep.git:**

  **Human #1** (3798df1516) — warm_method=**both** (hook vs action)
  - hook: delta mode, reindexed=1, carried=222, 21 B materialized, 0.2362s
  - action: bundle 1.4 MiB, 0.5675s
  - **comparison**: hook 0.2362s vs action 0.5675s → **hook faster** (ratio 2.40×)

---

## a844951d — 2026-06-26 20:07 UTC

Ran 2026-06-26 20:07 UTC → 2026-06-26 20:07 UTC (10s)

> Cache experiment over 2 repo(s) [fd.git, ripgrep.git]: 2 agent pass(es) per repo (pass 1 = COLD/builds the cache, later passes = WARM/steady state); methods compared: naive, blobless, agentcache; the agent edits 2.0% of source files each pass (seed 1000); 1 teammate (human) commit(s) interleaved between agent passes, each pre-warming the cache via the post-receive hook (the next agent stays warm); each agent pass runs in a fresh, disposable Docker container. Agent network cost is measured end-to-end through a byte-counting proxy; each human step records its own wall time and the cache-rebuild load it triggered (delta vs full, files reindexed/carried-forward, bytes materialized).

| Repo | files | naive (warm) | blobless (warm) | agentcache (warm) | agentcache cold | win vs naive |
|------|------:|-------------:|----------------:|------------------:|----------------:|-------------:|
| fd.git | 57 | 142.1 KiB | 12.9 KiB | 11.3 KiB | 1.1 MiB | 12.6× |
| ripgrep.git | 222 | 649.8 KiB | 51.5 KiB | 42.6 KiB | 1.5 MiB | 15.3× |

### Human steps

**fd.git:**

  **Human #1** (3b935d3019) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=57, 21 B materialized, 0.052s

**ripgrep.git:**

  **Human #1** (1199c8b53d) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=222, 21 B materialized, 0.1626s

---

## Raw data

The raw JSON files backing this report are committed at `experiments/results/harness/`:

- [`263c0e79.json`](results/harness/263c0e79.json)
- [`a827b997.json`](results/harness/a827b997.json)
- [`a844951d.json`](results/harness/a844951d.json)

