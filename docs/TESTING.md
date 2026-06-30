# Testing, results & the harness

A full benchmarking harness, a one-shot measured trial for *your* repo, and the
committed results from this installation.

**Diagnostic, not a marketing benchmark.** This harness exists to find where
agentcache helps *and* where plain blobless is already enough — not to win a
demo. On many repos the honest answer is "blobless is enough"; the harness is how
you tell which case you're in. For the conceptual comparison, see
[Blobless vs AgentCache](BLOBLESS.md); for the mechanism, see
[How AgentCache works](HOW_IT_WORKS.md).

## The test harness

This repo ships a full benchmarking harness that measures the savings on real
repositories, and a web UI to run and visualize experiments.

```bash
pip install -r requirements.txt
python -m experiments.prep                       # configure repos + build bundles
uvicorn testharness.app:app --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080  → "Experiments" tab
```

It measures three access strategies side by side — **naive**
(`git clone --depth=1`), **blobless** (`git clone --filter=blob:none --depth=1`),
and **agentcache** (blobless + CDN bundle + `resolve` → one batched fetch). See
the [strategy table in Blobless vs AgentCache](BLOBLESS.md#three-strategies) for
the full definitions.

## Try it on your repo

Want real, *measured* numbers for your own repo instead of a static guess? Run
the one-shot trial. It mirrors the repo, stands up a byte-counting proxy, and
reports the actual network bytes for naive vs blobless vs agentcache (one cold
pass to seed the artifacts, then the warm steady state) plus an honest verdict.

```bash
# From your repo's root (default = current directory):
python scripts/try_agentcache.py

# Or point it anywhere — local path or a GitHub URL:
python scripts/try_agentcache.py /path/to/your-repo
python scripts/try_agentcache.py https://github.com/owner/name

# From anywhere, no checkout needed (sets up a throwaway venv):
curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | bash
```

This measures your repo with a simulated agent editing 2% of files.

**It offers to set itself up — only when warranted.** If (and only if) the
measured verdict is **`agentcache worthwhile`** and you ran it inside a local
working clone, the trial then asks whether to scaffold the adoption files for
you. On yes it *creates* (never overwrites) up to three files in your working
tree — `.github/workflows/agentcache.yml` (only for GitHub remotes, from the
[adopter workflow template](adopter-workflow.yml)), `AGENTS.md`
(from [`ADOPTER_AGENTS_TEMPLATE.md`](ADOPTER_AGENTS_TEMPLATE.md)), and
`.agentcache` — then tells you to review, commit, and push. **It never runs
`git add`, `commit`, or `push` for you, and never touches a remote.** For any
other verdict (or a non-worktree target) it does nothing but report. Flags:
`--yes` scaffolds without prompting, `--no-install` disables the offer, `--json`
is non-interactive (the offer is reported, not performed). The prompt reads from
`/dev/tty`, so it still works through `curl … | bash`.

**Honest cold-cost caveat.** The cold column is not apples-to-apples and the
trial says so: agentcache cold delivers **full history**, whereas the blobless
column is a `--depth=1` *shallow* clone — different products. In production the
bootstrap bundle is built **once per commit and CDN-cached**, so the cold cost is
paid once and amortized; judge the steady-state **warm** saving on its own terms.

**Predict before you measure.** For a cheap static read on whether it's even
worth measuring, run the predictor first — it reports
`agentcache worthwhile` / `blobless is enough` / `inconclusive — measure to be
sure` from repo shape alone (deliberately conservative; it abstains rather than
over-promise):

```bash
python scripts/assess_repo.py /path/to/your-repo     # or a https://github.com/... URL
```

## Serving the harness over a tailnet (Tailscale)

To make the harness reachable across your tailnet, set `AGENTCACHE_WEB_HOST`
to your tailnet IP and pass it to uvicorn:

```bash
AGENTCACHE_WEB_HOST=100.107.70.97 AGENTCACHE_WEB_PORT=8090 \
  uvicorn testharness.app:app --host 100.107.70.97 --port 8090
```

`start.sh` picks up both variables automatically, so
`AGENTCACHE_WEB_HOST=100.107.70.97 bash testharness/start.sh --port 8090`
works too.

**Persistent service (systemd `--user`):**

```ini
# ~/.config/systemd/user/agentcache-harness.service
[Unit]
Description=AgentCache Test Harness

[Service]
WorkingDirectory=/path/to/AgentCache
Environment=AGENTCACHE_WEB_HOST=100.107.70.97
Environment=AGENTCACHE_WEB_PORT=8090
ExecStart=/path/to/AgentCache/.venv/bin/uvicorn \
    testharness.app:app --host 100.107.70.97 --port 8090
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now agentcache-harness
# To survive logout / start at boot without an interactive session:
sudo loginctl enable-linger $USER
```

**Optional HTTPS overlay via Tailscale:**

```bash
sudo tailscale serve --bg --https=8443 http://100.107.70.97:8090
```

## What the harness measures

- **Every pass runs in a fresh, disposable Docker container** by default (true
  VM-like isolation), and **all git traffic is routed through a byte-counting
  proxy**, so the network numbers are real, attributed per run.
- The **Experiments** view runs many projects × many passes and charts
  **cold (1st visit) vs warm (steady state)**, the **naive/blobless/agentcache**
  comparison, and a **per-pass timeline** — including optional **human commits**
  that move HEAD to show how a teammate's push invalidates (or, via the hook,
  pre-warms) the cache.
- Honest cold start: on a genuinely fresh repo there is **no cache and no
  bundle yet**, so the first agentcache pass pays full network cost (as much as
  or more than blobless) — only *then* does it drop to the warm steady state.

Headline result (warm steady state, agent edits 2% of files):

| Repo | naive | blobless | **agentcache** | vs naive |
|------|------:|---------:|---------------:|---------:|
| anthropic-cookbook | 153 MiB | 44 KiB | **16 KiB** | ~9,979× |
| ohmyzsh | 3 MiB | 61 KiB | **6 KiB** | 570× |
| prettier | 6 MiB | 549 KiB | **44 KiB** | 150× |
| cpython | 43 MiB | 586 KiB | **372 KiB** | 118× |

These are **warm** numbers. The first (cold) visit to a fresh repo pays full
network cost — honestly measured — before dropping to the figures above, and on
small/lean repos blobless alone is already close enough that agentcache adds
little (see [Blobless vs AgentCache](BLOBLESS.md)).

Run the studies headless instead of via the web:

```bash
python -m experiments.exp1_cold_warm     # cold vs warm across the repo fleet
python -m experiments.exp2_taint         # does a non-aware agent corrupt the cache? (no)
python -m experiments.exp3_hook_update   # does a human push keep the cache current? (yes)
```

## Results from this installation

**→ [`experiments/RECENT.md`](../experiments/RECENT.md) — a readable digest of the
most recent experiment runs** (what was done, per-repo win-vs-naive tables, the
per-human-commit cache-rebuild load, and the hook-vs-GitHub-Action warm
comparison). Regenerate any time with
`python scripts/render_experiment_report.py`.

The numbers above were produced by running the studies **on this installation**
— a 15-repo polyglot fleet (cpython, django, go, git, redis, openai/codex,
anthropic-sdk-python, anthropic-cookbook, jq, bat, ripgrep, prettier, ohmyzsh,
git-lfs, fd). The raw artifacts are committed in
[`experiments/results/`](../experiments/results/):

- [`RECENT.md`](../experiments/RECENT.md) — digest of the latest harness runs
  (raw JSON under [`results/harness/`](../experiments/results/harness/))
- [`SUMMARY.md`](../experiments/results/SUMMARY.md) — the full write-up
- [`exp1_cold_warm.json`](../experiments/results/exp1_cold_warm.json) — cold vs warm
  network bytes per repo × method (the data behind the headline table)
- [`exp2_taint.json`](../experiments/results/exp2_taint.json) — cache isolation:
  **PRISTINE** on every repo after unaware agents run
- [`exp3_hook_update.json`](../experiments/results/exp3_hook_update.json) — a human
  push keeps the cache current: **PASS**

**agentcache was the bandwidth winner on all 15 repos** (17×–9,979× less than a
naive clone) at warm steady state, while still delivering full history in a
single round-trip. **But the cold cost is real:** the first (cold) visit to a
fresh repo pays full network cost — honestly measured — before dropping to the
warm numbers shown.

To regenerate them yourself, run the three `experiments.*` commands above (or use
the **Experiments** tab in the web UI) and the JSON files will be rewritten.
