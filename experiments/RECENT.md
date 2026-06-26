# AgentCache experiment diagnostic

This is a **diagnostic report for maintainers**, not a marketing document. Its purpose is to surface both where agentcache helps AND where it falls short vs naive and blobless, so that weak spots can be found and fixed. Data comes from real runs of the [test harness](../testharness/) measuring three git-fetch strategies — **naive** (full clone), **blobless** (`--filter=blob:none`), and **agentcache** (targeted blob fetch via the pre-built manifest + symbol cache). Each experiment runs multiple agent passes per repo: **pass 1 is COLD** (agentcache downloads its bootstrap bundle and builds its cache from scratch), and **later passes are WARM** (only the requested blobs are fetched). **Column caveat:** the agentcache cold column delivers *full history* (full-history blobless clone, no depth limit); the blobless column uses `--depth=1` (shallow, no history) — the two cold columns are not directly comparable on a bytes basis. **Framing:** naive is the easy strawman; the real test is agentcache vs blobless. agentcache carries two costs that blobless does not: (1) a large COLD bootstrap bundle whose size scales with repo history depth, and (2) a per-commit server-side warm overhead on every human push. Both costs are exposed in detail below.

Regenerate with: `python scripts/render_experiment_report.py`

See also the headless studies: [SUMMARY.md](results/SUMMARY.md) · [exp1 (cold vs warm)](results/exp1_cold_warm.json) · [exp2 (cache taint)](results/exp2_taint.json) · [exp3 (hook update)](results/exp3_hook_update.json)

---

## Where agentcache has holes (improvement targets)

The entries below are engineering improvement targets, not edge-cases. Data is aggregated across all featured experiments; repos appearing in multiple runs are de-duplicated (worst value kept).

### 1. Full-history cold cost (read the caveat — this is NOT a clean defect)

The table shows agentcache's cold-start bytes ÷ blobless's. **Two measurement artifacts inflate this ratio — do not read it as pure overhead:**
1. **Full-history vs shallow.** agentcache's cold pass is a *full-history* blobless clone — it delivers complete history (agentcache's core promise). The blobless column is a `--depth=1` *shallow* clone with no history. This compares two different products.
2. **Un-amortized vs CDN-cached.** This is one cold agent paying the full first-visit cost. In production the bootstrap bundle is built once per commit and served as an immutable CDN-cached file reused by every agent on that commit — a per-commit cost, not per-agent.

**The genuine, narrower signal:** on deep-history repos the full-history payload (commits+trees) is large, and the per-commit bundle *artifact* scales with history. The real improvement target is a **base bundle + thin per-commit incremental** (chained via `--bundle-uri`), which shrinks the per-commit artifact from O(history) to O(delta) *without* losing full history. Also: the harness should measure the production-realistic cold-WITH-bundle / per-commit-amortized cost (today it hardwires cold⇒no-bundle), and an apples-to-apples arm (agentcache vs *full-history* blobless, not depth-1).

| Repo | agentcache cold (full history) | blobless cold (depth-1 shallow) | ratio (agentcache ÷ blobless) |
|------|-------------------------------:|--------------------------------:|------------------------------:|
| git.git | 105.6 MiB | 440.8 KiB | 245× |
| cpython.git | 116.5 MiB | 699.6 KiB | 171× |
| fd.git | 1.1 MiB | 7.4 KiB | 147× |
| git-lfs.git | 5.8 MiB | 63.2 KiB | 94× |
| ohmyzsh.git | 5.3 MiB | 67.2 KiB | 81× |
| django.git | 39.1 MiB | 543.7 KiB | 74× |
| go.git | 85.4 MiB | 1.3 MiB | 68× |
| redis.git | 10.5 MiB | 199.1 KiB | 54× |
| bat.git | 2.3 MiB | 55.7 KiB | 43× |
| ripgrep.git | 1.5 MiB | 43.1 KiB | 37× |
| prettier.git | 16.6 MiB | 591.5 KiB | 29× |
| codex.git | 19.6 MiB | 931.8 KiB | 22× |
| anthropic-cookbook.git | 557.4 KiB | 43.6 KiB | 13× |
| jq.git | 1.2 MiB | 106.0 KiB | 12× |
| anthropic-sdk-python.git | 858.9 KiB | 95.8 KiB | 9× |

> **Note (see caveats above):** Worst case is **git.git** at 105.6 MiB full-history cold vs 440.8 KiB for depth-1 blobless (245× ratio). This ratio is inflated by the full-history vs depth-1 mismatch and the un-amortized single-agent cost. The real improvement lever is a **base bundle + thin per-commit incremental** (via `--bundle-uri`), reducing per-commit artifact size from O(history) to O(delta) without losing full history.

### 2. Marginal warm win (< 25% saving vs blobless)

Repos where agentcache's warm-pass byte saving over blobless is small. The vs-naive win is large, but that is the easy case; if blobless already fetches only a few blobs, agentcache adds little.

| Repo | blobless warm | agentcache warm | saving vs blobless |
|------|-------------:|-----------------:|--------------------:|
| fd.git | 12.9 KiB | 11.3 KiB | 12.4% |
| ripgrep.git | 51.5 KiB | 42.6 KiB | 17.4% |
| codex.git | 968.8 KiB | 745.2 KiB | 23.1% |
| redis.git | 249.2 KiB | 187.5 KiB | 24.8% |

> **TODO — improve warm selectivity:** On lean repos like **fd.git** the warm saving is only 12.4% vs blobless. Consider skipping or opt-in-only agentcache on repos where the agent's file edit set is small relative to total blobs.

### 3. Impractical break-even (> 100 warm passes)

Repos where agentcache needs more than 100 warm passes to repay its cold-start overhead vs blobless. In realistic agent workflows this break-even is rarely if ever reached, making blobless the better default for these repos.

| Repo | break-even passes | cold overhead vs blobless | warm saved/pass |
|------|------------------:|---------------------------:|----------------:|
| fd.git | 715 | 1.1 MiB | 1.5 KiB |
| git.git | 682 | 105.2 MiB | 158.0 KiB |
| cpython.git | 541 | 115.8 MiB | 219.4 KiB |
| git-lfs.git | 203 | 5.7 MiB | 29.0 KiB |
| redis.git | 172 | 10.3 MiB | 61.7 KiB |
| ripgrep.git | 172 | 1.5 MiB | 8.9 KiB |
| go.git | 148 | 84.1 MiB | 585.4 KiB |
| django.git | 108 | 38.6 MiB | 366.7 KiB |

> **TODO — gate on repo heuristics:** 8 repo(s) have break-even > 100 passes (worst: **fd.git** at 715 passes). For these repos, blobless is the practical default. Fix: gate agentcache on a repo-size or history-depth heuristic, or reduce the bundle footprint on shallow histories.

### 4. Expensive per-commit warm overhead (hook wall > 1 s)

Every human push triggers a server-side index rebuild that naive and blobless don't pay at all. High hook wall times are a continuous maintenance tax on developer velocity.

| Repo | max hook wall (s) |
|------|------------------:|
| go.git | 15.081s |
| anthropic-cookbook.git | 7.330s |
| cpython.git | 6.483s |
| codex.git | 4.601s |
| django.git | 2.812s |
| git.git | 1.888s |
| prettier.git | 1.613s |
| redis.git | 1.534s |

> **TODO — profile and async-ify the hook:** 8 repo(s) have hook wall > 1 s per human push (worst: **go.git** at 15.1s). Profile the hot path for large symbol-count repos; consider async or deferred indexing so the push completes immediately.

---

## 6d89556c — 2026-06-26 22:39 UTC

Ran 2026-06-26 22:22 UTC → 2026-06-26 22:39 UTC (988s)

> Cache experiment over 15 repo(s) [anthropic-cookbook.git, anthropic-sdk-python.git, bat.git, codex.git, cpython.git, django.git, fd.git, git-lfs.git, git.git, go.git, jq.git, ohmyzsh.git, prettier.git, redis.git, ripgrep.git]: 3 agent pass(es) per repo (pass 1 = COLD/builds the cache, later passes = WARM/steady state); methods compared: naive, blobless, agentcache; the agent edits 3.0% of source files each pass (seed 4244); 2 teammate (human) commit(s) interleaved between agent passes, cache warmed after each human commit via the 'hook' mechanism (hook = in-process server post-receive; action = github-action subprocess incl. blobless bundle; both = run and compare); the next agent stays warm; each agent pass runs in a fresh, disposable Docker container. Agent network cost is measured end-to-end through a byte-counting proxy; each human step records its own wall time and the cache-rebuild load it triggered (delta vs full, files reindexed/carried-forward, bytes materialized).

| Repo | files | naive (warm) | blobless (warm) | agentcache (warm) | agentcache cold | win vs naive |
|------|------:|-------------:|----------------:|------------------:|----------------:|-------------:|
| anthropic-cookbook.git | 574 | 153.3 MiB | 49.3 KiB | 22.0 KiB | 559.0 KiB | 7145.4× |
| anthropic-sdk-python.git | 1189 | 1.1 MiB | 94.3 KiB | 46.0 KiB | 867.4 KiB | 24.2× |
| bat.git | 1004 | 2.1 MiB | 55.3 KiB | 10.8 KiB | 2.3 MiB | 200.7× |
| codex.git | 5259 | 10.3 MiB | 968.8 KiB | 745.2 KiB | 19.7 MiB | 14.2× |
| cpython.git | 5801 | 43.0 MiB | 826.2 KiB | 606.9 KiB | 116.5 MiB | 72.6× |
| django.git | 7070 | 12.0 MiB | 525.2 KiB | 158.5 KiB | 39.1 MiB | 77.3× |
| fd.git | 57 | 142.1 KiB | 6.9 KiB | 5.4 KiB | 1.1 MiB | 26.4× |
| git-lfs.git | 650 | 877.7 KiB | 62.1 KiB | 33.2 KiB | 5.8 MiB | 26.5× |
| git.git | 4765 | 12.4 MiB | 431.1 KiB | 273.1 KiB | 105.6 MiB | 46.6× |
| go.git | 15596 | 36.1 MiB | 1.3 MiB | 698.3 KiB | 85.4 MiB | 52.9× |
| jq.git | 429 | 1.3 MiB | 29.0 KiB | 15.0 KiB | 1.2 MiB | 88.2× |
| ohmyzsh.git | 1091 | 3.3 MiB | 71.1 KiB | 16.3 KiB | 5.3 MiB | 209.1× |
| prettier.git | 9373 | 6.4 MiB | 570.0 KiB | 53.8 KiB | 16.6 MiB | 121.7× |
| redis.git | 1818 | 4.8 MiB | 249.2 KiB | 187.5 KiB | 10.5 MiB | 26.3× |
| ripgrep.git | 222 | 649.8 KiB | 46.0 KiB | 36.9 KiB | 1.5 MiB | 17.6× |

### Cold start across the three approaches

> **Column caveat:** agentcache cold = full-history blobless clone; blobless cold = `--depth=1` shallow clone (no history). These columns are not directly comparable on a bytes basis.

| Repo | naive cold | blobless cold (depth-1 shallow) | agentcache cold (full history) | agentcache cold ÷ blobless |
|------|----------:|-------------:|----------------:|---------------------------:|
| anthropic-cookbook.git | 153.3 MiB | 45.6 KiB | 559.0 KiB | 12× |
| anthropic-sdk-python.git | 1.1 MiB | 107.2 KiB | 867.4 KiB | 8× |
| bat.git | 2.1 MiB | 56.7 KiB | 2.3 MiB | 42× |
| codex.git | 10.3 MiB | 986.9 KiB | 19.7 MiB | 20× |
| cpython.git | 43.0 MiB | 699.6 KiB | 116.5 MiB | 171× |
| django.git | 12.0 MiB | 543.7 KiB | 39.1 MiB | 74× |
| fd.git | 142.8 KiB | 9.3 KiB | 1.1 MiB | 117× |
| git-lfs.git | 878.3 KiB | 63.2 KiB | 5.8 MiB | 94× |
| git.git | 12.4 MiB | 440.8 KiB | 105.6 MiB | 245× |
| go.git | 36.1 MiB | 1.3 MiB | 85.4 MiB | 68× |
| jq.git | 1.3 MiB | 106.0 KiB | 1.2 MiB | 12× |
| ohmyzsh.git | 3.3 MiB | 67.2 KiB | 5.3 MiB | 81× |
| prettier.git | 6.4 MiB | 591.5 KiB | 16.6 MiB | 29× |
| redis.git | 4.8 MiB | 199.1 KiB | 10.5 MiB | 54× |
| ripgrep.git | 650.3 KiB | 47.7 KiB | 1.5 MiB | 33× |

### Cost / benefit vs blobless (the honest competitor)

| Repo | warm saved/pass vs blobless | warm vs naive | cold overhead vs blobless | break-even (warm passes) | verdict |
|------|----------------------------:|:--------------:|---------------------------:|:------------------------:|:--------|
| anthropic-cookbook.git | 27.4 KiB (55.5%) | 7145.4× | 513.3 KiB | 19 | break-even vs blobless: ~19 warm passes (513.3 KiB ÷ 27.4 KiB/pass) |
| anthropic-sdk-python.git | 48.3 KiB (51.2%) | 24.2× | 760.2 KiB | 16 | break-even vs blobless: ~16 warm passes (760.2 KiB ÷ 48.3 KiB/pass) |
| bat.git | 44.6 KiB (80.6%) | 200.7× | 2.3 MiB | 53 | break-even vs blobless: ~53 warm passes (2.3 MiB ÷ 44.6 KiB/pass) |
| codex.git | 223.6 KiB (23.1%) | 14.2× | 18.7 MiB | 86 | break-even vs blobless: ~86 warm passes (18.7 MiB ÷ 223.6 KiB/pass) |
| cpython.git | 219.4 KiB (26.6%) | 72.6× | 115.8 MiB | 541 | agentcache does NOT repay its cold cost vs blobless within ~541 passes — blobless preferred |
| django.git | 366.7 KiB (69.8%) | 77.3× | 38.6 MiB | 108 | agentcache does NOT repay its cold cost vs blobless within ~108 passes — blobless preferred |
| fd.git | 1.5 KiB (21.9%) | 26.4× | 1.1 MiB | 715 | agentcache does NOT repay its cold cost vs blobless within ~715 passes — blobless preferred |
| git-lfs.git | 29.0 KiB (46.6%) | 26.5× | 5.7 MiB | 203 | agentcache does NOT repay its cold cost vs blobless within ~203 passes — blobless preferred |
| git.git | 158.0 KiB (36.6%) | 46.6× | 105.2 MiB | 682 | agentcache does NOT repay its cold cost vs blobless within ~682 passes — blobless preferred |
| go.git | 585.4 KiB (45.6%) | 52.9× | 84.1 MiB | 148 | agentcache does NOT repay its cold cost vs blobless within ~148 passes — blobless preferred |
| jq.git | 14.0 KiB (48.4%) | 88.2× | 1.1 MiB | 82 | break-even vs blobless: ~82 warm passes (1.1 MiB ÷ 14.0 KiB/pass) |
| ohmyzsh.git | 54.8 KiB (77.1%) | 209.1× | 5.2 MiB | 98 | break-even vs blobless: ~98 warm passes (5.2 MiB ÷ 54.8 KiB/pass) |
| prettier.git | 516.2 KiB (90.6%) | 121.7× | 16.1 MiB | 32 | break-even vs blobless: ~32 warm passes (16.1 MiB ÷ 516.2 KiB/pass) |
| redis.git | 61.7 KiB (24.8%) | 26.3× | 10.3 MiB | 172 | agentcache does NOT repay its cold cost vs blobless within ~172 passes — blobless preferred |
| ripgrep.git | 9.1 KiB (19.8%) | 17.6× | 1.5 MiB | 169 | agentcache does NOT repay its cold cost vs blobless within ~169 passes — blobless preferred |

> Break-even vs blobless ranges from ~16 passes (anthropic-sdk-python) to ~715 passes (fd). 8 repo(s) exceed 100 passes — blobless is the practical default for those.

### Server-side warm overhead (the maintenance cost)

Every human commit triggers a server-side cache rebuild (CPU + storage) so the next agent finds a warm cache. This is a maintenance cost that **naive** and **blobless** don't pay.

| Repo | warm_method | hook wall | action wall | bundle bytes |
|------|:-----------:|----------:|------------:|-------------:|
| anthropic-cookbook.git | hook | 6.492s | — | — |
| anthropic-cookbook.git | hook | 5.779s | — | — |
| anthropic-sdk-python.git | hook | 0.403s | — | — |
| anthropic-sdk-python.git | hook | 0.449s | — | — |
| bat.git | hook | 0.375s | — | — |
| bat.git | hook | 0.342s | — | — |
| codex.git | hook | 4.459s | — | — |
| codex.git | hook | 4.292s | — | — |
| cpython.git | hook | 6.483s | — | — |
| cpython.git | hook | 5.863s | — | — |
| django.git | hook | 2.812s | — | — |
| django.git | hook | 2.690s | — | — |
| fd.git | hook | 0.066s | — | — |
| fd.git | hook | 0.044s | — | — |
| git-lfs.git | hook | 0.304s | — | — |
| git-lfs.git | hook | 0.208s | — | — |
| git.git | hook | 1.578s | — | — |
| git.git | hook | 1.888s | — | — |
| go.git | hook | 15.081s | — | — |
| go.git | hook | 13.263s | — | — |
| jq.git | hook | 0.271s | — | — |
| jq.git | hook | 0.174s | — | — |
| ohmyzsh.git | hook | 0.561s | — | — |
| ohmyzsh.git | hook | 0.638s | — | — |
| prettier.git | hook | 1.613s | — | — |
| prettier.git | hook | 1.341s | — | — |
| redis.git | hook | 1.534s | — | — |
| redis.git | hook | 1.429s | — | — |
| ripgrep.git | hook | 0.176s | — | — |
| ripgrep.git | hook | 0.177s | — | — |

> Server-side time and bundle bytes are CPU + storage cost paid on every human push — separate from the agent network bytes measured in the results table above. The in-process hook path is typically 2–10× faster than the GitHub Action path (subprocess + blobless bundle rebuild). Delta indexing minimises re-work by re-ctags-ing only changed files and carrying the rest forward.

### Human steps

**anthropic-cookbook.git:**

  **Human #1** (f5bc2e7bd8) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=574, 21 B materialized, 6.4925s

  **Human #2** (9485a19f03) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=575, 21 B materialized, 5.779s

**anthropic-sdk-python.git:**

  **Human #1** (47797da776) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1189, 21 B materialized, 0.4027s

  **Human #2** (9dad653824) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1190, 21 B materialized, 0.4486s

**bat.git:**

  **Human #1** (46a5e3feb3) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1004, 21 B materialized, 0.375s

  **Human #2** (5a77982030) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1005, 21 B materialized, 0.3417s

**codex.git:**

  **Human #1** (e8c9aad9af) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=5259, 21 B materialized, 4.4587s

  **Human #2** (4e0213fad6) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=5260, 21 B materialized, 4.2919s

**cpython.git:**

  **Human #1** (eb7a7dc269) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=5801, 21 B materialized, 6.4831s

  **Human #2** (4779b41bb5) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=5802, 21 B materialized, 5.8633s

**django.git:**

  **Human #1** (4a1b8712bf) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=7070, 21 B materialized, 2.8116s

  **Human #2** (1aa4a45372) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=7071, 21 B materialized, 2.6902s

**fd.git:**

  **Human #1** (67c15925d5) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=57, 21 B materialized, 0.0657s

  **Human #2** (93ec7e6d3c) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=58, 21 B materialized, 0.0436s

**git-lfs.git:**

  **Human #1** (adca14a041) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=650, 21 B materialized, 0.3041s

  **Human #2** (1f57b959fb) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=651, 21 B materialized, 0.2082s

**git.git:**

  **Human #1** (69417113bc) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=4765, 21 B materialized, 1.5781s

  **Human #2** (0cbb5719cf) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=4766, 21 B materialized, 1.8881s

**go.git:**

  **Human #1** (504dd8c687) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=15596, 21 B materialized, 15.081s

  **Human #2** (403fa13cdb) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=15597, 21 B materialized, 13.2627s

**jq.git:**

  **Human #1** (f736403b7c) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=429, 21 B materialized, 0.2711s

  **Human #2** (1dab92a9f4) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=430, 21 B materialized, 0.174s

**ohmyzsh.git:**

  **Human #1** (279ebc5b63) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1091, 21 B materialized, 0.5611s

  **Human #2** (bcd85b9c72) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1092, 21 B materialized, 0.6376s

**prettier.git:**

  **Human #1** (18ee3d66ce) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=9373, 21 B materialized, 1.6127s

  **Human #2** (ab1351d55c) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=9374, 21 B materialized, 1.3407s

**redis.git:**

  **Human #1** (229cc2a880) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1818, 21 B materialized, 1.5343s

  **Human #2** (b54f1ed548) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=1819, 21 B materialized, 1.4287s

**ripgrep.git:**

  **Human #1** (f61ddfac1d) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=222, 21 B materialized, 0.1765s

  **Human #2** (079d4eeb31) — warm_method=hook
  - hook: delta mode, reindexed=1, carried=223, 21 B materialized, 0.177s

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

### Cold start across the three approaches

> **Column caveat:** agentcache cold = full-history blobless clone; blobless cold = `--depth=1` shallow clone (no history). These columns are not directly comparable on a bytes basis.

| Repo | naive cold | blobless cold (depth-1 shallow) | agentcache cold (full history) | agentcache cold ÷ blobless |
|------|----------:|-------------:|----------------:|---------------------------:|
| anthropic-cookbook.git | 153.3 MiB | 43.6 KiB | 557.4 KiB | 13× |
| anthropic-sdk-python.git | 1.1 MiB | 95.8 KiB | 858.9 KiB | 9× |
| bat.git | 2.1 MiB | 55.7 KiB | 2.3 MiB | 43× |
| codex.git | 10.3 MiB | 931.8 KiB | 19.6 MiB | 22× |

### Cost / benefit vs blobless (the honest competitor)

| Repo | warm saved/pass vs blobless | warm vs naive | cold overhead vs blobless | break-even (warm passes) | verdict |
|------|----------------------------:|:--------------:|---------------------------:|:------------------------:|:--------|
| anthropic-cookbook.git | 27.0 KiB (63.7%) | 10201.5× | 513.8 KiB | 20 | break-even vs blobless: ~20 warm passes (513.8 KiB ÷ 27.0 KiB/pass) |
| anthropic-sdk-python.git | 44.7 KiB (58.1%) | 34.5× | 763.1 KiB | 18 | break-even vs blobless: ~18 warm passes (763.1 KiB ÷ 44.7 KiB/pass) |
| bat.git | 44.2 KiB (82.0%) | 222.1× | 2.3 MiB | 53 | break-even vs blobless: ~53 warm passes (2.3 MiB ÷ 44.2 KiB/pass) |
| codex.git | 215.6 KiB (24.6%) | 16.0× | 18.7 MiB | 89 | break-even vs blobless: ~89 warm passes (18.7 MiB ÷ 215.6 KiB/pass) |

> Break-even vs blobless ranges from ~18 passes (anthropic-sdk-python) to ~89 passes (codex).

### Server-side warm overhead (the maintenance cost)

Every human commit triggers a server-side cache rebuild (CPU + storage) so the next agent finds a warm cache. This is a maintenance cost that **naive** and **blobless** don't pay.

| Repo | warm_method | hook wall | action wall | bundle bytes |
|------|:-----------:|----------:|------------:|-------------:|
| anthropic-cookbook.git | hook | 7.330s | — | — |
| anthropic-sdk-python.git | hook | 0.376s | — | — |
| bat.git | hook | 0.402s | — | — |
| codex.git | hook | 4.601s | — | — |

> Server-side time and bundle bytes are CPU + storage cost paid on every human push — separate from the agent network bytes measured in the results table above. The in-process hook path is typically 2–10× faster than the GitHub Action path (subprocess + blobless bundle rebuild). Delta indexing minimises re-work by re-ctags-ing only changed files and carrying the rest forward.

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

### Cold start across the three approaches

> **Column caveat:** agentcache cold = full-history blobless clone; blobless cold = `--depth=1` shallow clone (no history). These columns are not directly comparable on a bytes basis.

| Repo | naive cold | blobless cold (depth-1 shallow) | agentcache cold (full history) | agentcache cold ÷ blobless |
|------|----------:|-------------:|----------------:|---------------------------:|
| fd.git | 142.8 KiB | 7.4 KiB | 1.1 MiB | 147× |
| ripgrep.git | 650.3 KiB | 43.1 KiB | 1.5 MiB | 37× |

### Cost / benefit vs blobless (the honest competitor)

| Repo | warm saved/pass vs blobless | warm vs naive | cold overhead vs blobless | break-even (warm passes) | verdict |
|------|----------------------------:|:--------------:|---------------------------:|:------------------------:|:--------|
| fd.git | 1.6 KiB (12.4%) | 12.6× | 1.1 MiB | 677 | agentcache does NOT repay its cold cost vs blobless within ~677 passes — blobless preferred |
| ripgrep.git | 8.9 KiB (17.4%) | 15.3× | 1.5 MiB | 172 | agentcache does NOT repay its cold cost vs blobless within ~172 passes — blobless preferred |

> Break-even vs blobless ranges from ~172 passes (ripgrep) to ~677 passes (fd). 2 repo(s) exceed 100 passes — blobless is the practical default for those.

### Server-side warm overhead (the maintenance cost)

Every human commit triggers a server-side cache rebuild (CPU + storage) so the next agent finds a warm cache. This is a maintenance cost that **naive** and **blobless** don't pay.

| Repo | warm_method | hook wall | action wall | bundle bytes |
|------|:-----------:|----------:|------------:|-------------:|
| fd.git | both | 0.072s | 0.329s | 1.1 MiB |
| ripgrep.git | both | 0.236s | 0.568s | 1.4 MiB |

> Server-side time and bundle bytes are CPU + storage cost paid on every human push — separate from the agent network bytes measured in the results table above. The in-process hook path is typically 2–10× faster than the GitHub Action path (subprocess + blobless bundle rebuild). Delta indexing minimises re-work by re-ctags-ing only changed files and carrying the rest forward.

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

## Raw data

The raw JSON files backing this report are committed at `experiments/results/harness/`:

- [`263c0e79.json`](results/harness/263c0e79.json)
- [`6d89556c.json`](results/harness/6d89556c.json)
- [`a827b997.json`](results/harness/a827b997.json)

