# Testing, results & the harness

A full benchmarking harness, a one-shot measured trial for *your* repo, and the
committed results from this installation.

**Diagnostic, not a marketing benchmark.** This harness exists to find where
agentgitsmart helps *and* where plain blobless is already enough — not to win a
demo. On many repos the honest answer is "blobless is enough"; the harness is how
you tell which case you're in. For the conceptual comparison, see
[Blobless vs AgentGitSmart](BLOBLESS.md); for the mechanism, see
[How AgentGitSmart works](HOW_IT_WORKS.md).

## The test harness

This repo ships a full benchmarking harness that measures the savings on real
repositories, and a web UI to run and visualize experiments.

```bash
pip install -r requirements.txt
python -m experiments.prep                       # configure repos + build bundles
uvicorn testharness.app:app --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080  → "Experiments" tab
```

It measures four access strategies side by side — **naive**
(`git clone --depth=1`), **blobless** (`git clone --filter=blob:none --depth=1`,
N lazy per-file fetches), **blobless+batch** (aka *AgentGitSmartBlobless*: the same
blobless clone but ONE batched fetch keyed by OIDs read from the local trees —
**no server, hook, or side ref**), and **agentgitsmart** (blobless + CDN bundle +
`resolve` → one batched fetch). See the [strategy table in Blobless vs
AgentGitSmart](BLOBLESS.md#three-strategies) for the full definitions.

The **blobless+batch** arm exists to answer one question honestly: *how much does
the AgentGitSmart server actually add over a competent blobless client?* Its verdict
feeds the three-way recommendation (`blobless is enough` / `AgentGitSmartBlobless` /
`agentgitsmart worthwhile`).

> **Measured finding for blobless+batch — it is a LATENCY win, not a bandwidth
> one.** We tested the intuition that N lazy fetches each re-pay a *ref
> advertisement* (so batching would save bytes) directly, across git protocol
> **v2 and v0** and with up to **5,000 synthetic `refs/pull/*` refs injected**
> ([`exp4_ref_ads`](../experiments/exp4_ref_ads.py)). The result: batching saved
> **<5% of bytes in every cell** (often 0 or slightly negative) while always
> cutting round-trips. Under v2 (`ls-refs` filters the advertisement) each extra
> fetch is nearly free; under v0 more refs inflate the **clone**, which *both*
> arms pay equally. So blobless+batch ≈ blobless on **bytes**; its real win is
> **round-trips** (e.g. 20→1). The three-way verdict recommends it on that basis.

## Try it on your repo

Want real, *measured* numbers for your own repo instead of a static guess? Run
the one-shot trial. It mirrors the repo, stands up a byte-counting proxy, and
reports the actual network bytes for naive vs blobless vs agentgitsmart (one cold
pass to seed the artifacts, then the warm steady state) plus an honest verdict.

```bash
# From your repo's root (default = current directory):
python scripts/try_agentgitsmart.py

# Or point it anywhere — local path or a GitHub URL:
python scripts/try_agentgitsmart.py /path/to/your-repo
python scripts/try_agentgitsmart.py https://github.com/owner/name

# From anywhere, no checkout needed (sets up a throwaway venv):
curl -fsSL https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/scripts/try.sh | bash
```

This measures your repo with a simulated agent editing 2% of files.

**It offers to set itself up — only when warranted.** If (and only if) the
measured verdict is **`agentgitsmart worthwhile`** and you ran it inside a local
working clone, the trial then asks whether to scaffold the adoption files for
you. On yes it *creates* (never overwrites) up to four files in your working
tree — `.github/workflows/agentgitsmart.yml` (only for GitHub remotes, from the
[adopter workflow template](adopter-workflow.yml)), `AGENTS.md`
(from [`ADOPTER_AGENTS_TEMPLATE.md`](ADOPTER_AGENTS_TEMPLATE.md)), `CLAUDE.md`
(from [`ADOPTER_CLAUDE_TEMPLATE.md`](ADOPTER_CLAUDE_TEMPLATE.md), a thin pointer
that imports `AGENTS.md` so Claude Code sees the same instructions), and
`.agentgitsmart` — then tells you to review, commit, and push. **It never runs
`git add`, `commit`, or `push` for you, and never touches a remote.** For any
other verdict (or a non-worktree target) it does nothing but report. Flags:
`--yes` scaffolds without prompting, `--no-install` disables the offer, `--json`
is non-interactive (the offer is reported, not performed). The prompt reads from
`/dev/tty`, so it still works through `curl … | bash`.

**Honest cold-cost caveat.** The cold column is not apples-to-apples and the
trial says so: agentgitsmart cold delivers **full history**, whereas the blobless
column is a `--depth=1` *shallow* clone — different products. In production the
bootstrap bundle is built **once per commit and CDN-cached**, so the cold cost is
paid once and amortized; judge the steady-state **warm** saving on its own terms.

**Predict before you measure.** For a cheap static read on whether it's even
worth measuring, run the predictor first — it reports
`agentgitsmart worthwhile` / `blobless is enough` / `inconclusive — measure to be
sure` from repo shape alone (deliberately conservative; it abstains rather than
over-promise):

```bash
python scripts/assess_repo.py /path/to/your-repo     # or a https://github.com/... URL
```

## Serving the harness over a tailnet (Tailscale)

To make the harness reachable across your tailnet, set `AGENTGITSMART_WEB_HOST`
to your tailnet IP and pass it to uvicorn. Substitute your own address for
`100.x.y.z` below (`tailscale ip -4` prints it):

```bash
AGENTGITSMART_WEB_HOST=100.x.y.z AGENTGITSMART_WEB_PORT=8090 \
  uvicorn testharness.app:app --host 100.x.y.z --port 8090
```

`start.sh` picks up both variables automatically, so
`AGENTGITSMART_WEB_HOST=100.x.y.z bash testharness/start.sh --port 8090`
works too.

**Persistent service (systemd `--user`):**

```ini
# ~/.config/systemd/user/agentgitsmart-harness.service
[Unit]
Description=AgentGitSmart Test Harness

[Service]
WorkingDirectory=/path/to/AgentGitSmart
Environment=AGENTGITSMART_WEB_HOST=100.x.y.z
Environment=AGENTGITSMART_WEB_PORT=8090
ExecStart=/path/to/AgentGitSmart/.venv/bin/uvicorn \
    testharness.app:app --host 100.x.y.z --port 8090
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now agentgitsmart-harness
# To survive logout / start at boot without an interactive session:
sudo loginctl enable-linger $USER
```

**Optional HTTPS overlay via Tailscale:**

```bash
sudo tailscale serve --bg --https=8443 http://100.x.y.z:8090
```

## What the harness measures

- **Every pass runs in a fresh, disposable Docker container** by default (true
  VM-like isolation), and **all git traffic is routed through a byte-counting
  proxy**, so the network numbers are real, attributed per run.
- The **Experiments** view runs many projects × many passes and charts
  **cold (1st visit) vs warm (steady state)**, the **naive/blobless/agentgitsmart**
  comparison, and a **per-pass timeline** — including optional **human commits**
  that move HEAD to show how a teammate's push invalidates (or, via the hook,
  pre-warms) the cache.
- Honest cold start: on a genuinely fresh repo there is **no cache and no
  bundle yet**, so the first agentgitsmart pass pays full network cost (as much as
  or more than blobless) — only *then* does it drop to the warm steady state.

Headline result (warm steady state, agent edits 2% of files):

| Repo | naive | blobless | blobless+batch | **agentgitsmart** | vs naive |
|------|------:|---------:|---------------:|---------------:|---------:|
| anthropic-cookbook | 153 MiB | 45 KiB | 45 KiB | **16 KiB** | 9,562× |
| ohmyzsh | 3 MiB | 60 KiB | 61 KiB | **5 KiB** | 645× |
| prettier | 6 MiB | 548 KiB | 549 KiB | **42 KiB** | 154× |
| cpython | 43 MiB | 562 KiB | 562 KiB | **349 KiB** | 126× |

These are **warm** numbers. Note that **blobless+batch tracks blobless almost
exactly on bytes** — and [`exp4_ref_ads`](../experiments/exp4_ref_ads.py) confirms
that holds across protocol v0/v2 and thousands of injected refs (its win is
**round-trips**, not bandwidth) — while agentgitsmart keeps a real byte edge from
the CDN bundle. The full four-arm data for all 15 repos is in
[`exp1_cold_warm.json`](../experiments/results/exp1_cold_warm.json). The first (cold) visit to a fresh repo pays full
network cost — honestly measured — before dropping to the figures above, and on
small/lean repos blobless alone is already close enough that agentgitsmart adds
little (see [Blobless vs AgentGitSmart](BLOBLESS.md)).

Run the studies headless instead of via the web:

```bash
python -m experiments.exp1_cold_warm     # cold vs warm across the repo fleet (4 arms)
python -m experiments.exp2_taint         # does a non-aware agent corrupt the cache? (no)
python -m experiments.exp3_hook_update   # does a human push keep the cache current? (yes)
python -m experiments.exp4_ref_ads       # does batching save bytes or just round-trips? (round-trips)
```

## Results from this installation

**→ [`experiments/RECENT.md`](../experiments/RECENT.md) — a readable digest of the
most recent experiment runs** (what was done, per-repo win-vs-naive tables, the
per-human-commit cache-rebuild load, and the hook-vs-GitHub-Action warm
comparison). Regenerate any time with
`python scripts/render_experiment_report.py` — it reads the harness's own
`testharness/data/experiments/` (gitignored), so on a fresh clone it reports
"No complete experiments found" until you have run the harness yourself.

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

**agentgitsmart was the bandwidth winner on all 15 repos** (16×–9,562× less than a
naive clone) at warm steady state, while still delivering full history in a
single round-trip. **But the cold cost is real:** the first (cold) visit to a
fresh repo pays full network cost — honestly measured — before dropping to the
warm numbers shown.

To regenerate them yourself, run the three `experiments.*` commands above (or use
the **Experiments** tab in the web UI) and the JSON files will be rewritten.
