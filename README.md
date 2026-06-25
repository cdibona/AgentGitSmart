# agentcache

**Let AI coding agents work on a huge repo while downloading less than 1% of it.**

agentcache is server-side Git infrastructure. On every push, a `post-receive`
hook pre-computes per-commit *agent knowledge* and stores it as an **orphan
commit** under `refs/agent-cache/<source-commit-oid>` — out of the main history,
behind the same access control, cheap to fetch in isolation:

- **`manifest.json`** — flat `path → {oid, size, mode}` for the whole tree. Lets
  an agent plan which files to touch (and never fetch a 2 GB asset by accident)
  *without fetching any content*.
- **`symbols.json`** — `symbol → [{path, line, kind}]` from universal-ctags.
  Turns "grep the whole repo" (fetch everything) into a lookup that returns a
  few OIDs.

It also (optionally) emits a **blobless bootstrap bundle** per branch tip for
CDN-cacheable cold starts. A small query service answers `resolve(paths)` /
`symbol(name)` with blob OIDs + sizes, so the agent fetches only what it touches
**in one batched fetch** instead of N lazy round-trips.

The payoff, measured end-to-end (see [Try it](#try-it--the-test-harness)):
an agent editing 2% of a repo pulls **17×–10,000× less** over the network than a
normal `git clone`, while still getting full history.

---

## Getting started

### 👤 If you are a human

You have two jobs: **adopt** agentcache on a repo you control, and **tell your
agents to use it**.

**1. Adopt it on your Git server** (one time) — see [Install](#install-server):

```bash
# in the bare repo on your server
cp hooks/post-receive /srv/git/myrepo.git/hooks/post-receive
chmod +x /srv/git/myrepo.git/hooks/post-receive
git --git-dir=/srv/git/myrepo.git config uploadpack.allowFilter true
git --git-dir=/srv/git/myrepo.git config uploadpack.allowAnySHA1InWant true
AGENTCACHE_REPO_DIR=/srv/git/myrepo.git python -m agentcache.service   # the query API
```

From then on, every push keeps the cache current automatically — including
pushes from teammates who have never heard of agentcache (the hook runs
server-side).

**2. Make the repo agent-aware** so agents discover it on their own — drop an
`AGENTS.md` and a `.agentcache` file in the repo root (see
[Making your repo agent-aware](#making-your-repo-agent-aware)).

**3. Point your agent at it.** In any coding-agent session (Claude Code, Codex
CLI, Amplifier, Cursor, …), paste:

> Read https://raw.githubusercontent.com/cdibona/PackCache/main/README.md and
> follow the **"If you've been sent by a human"** section to use agentcache for
> this repo instead of a full clone.

…or, if the repo already has an `AGENTS.md`, simply: *"Read AGENTS.md — this
repo uses agentcache."*

### 🤖 If you've been sent by a human

You are an AI agent about to work on a repository that uses agentcache. **Do not
`git clone` the whole thing, and do not shallow-clone.** That defeats the entire
point. Instead, do a cold start that fetches only the blobs you will actually
read:

```bash
# 0. Discover the service. A PackCache-enabled repo advertises it in one of:
#      .agentcache        →  grep service_url .agentcache
#      AGENTS.md          →  the cold-start protocol + service URL
#    SVC = the agentcache service URL,  REPO_URL = the git remote,  COMMIT = HEAD

# 1. Blobless clone, seeded from the CDN bundle (NOT a direct clone of the bundle).
#    You get full history + every tree, but ZERO file content yet.
git clone --filter=blob:none --no-checkout \
    --bundle-uri="$SVC/bundles/$COMMIT.bundle" \
    "$REPO_URL" repo && cd repo

# 2. Plan against the side cache — no content fetched. Ask for the files you need
#    (or resolve a symbol first):  GET $SVC/cache/$COMMIT/symbol/<name>
OIDS=$(curl -s -XPOST "$SVC/cache/$COMMIT/resolve" \
        -H 'content-type: application/json' \
        -d '{"paths":["src/a.cpp","src/a.h"]}' | jq -r '.fetch_oids[]')

# 3. ONE batched fetch of exactly those blobs — not one round-trip per file.
git fetch origin $OIDS

# 4. Read by OID (no checkout needed):  git cat-file blob <oid>
#    …edit, build over the subset, commit, push a thin pack, destroy the VM.
```

Why this is correct, not a shortcut: the manifest gives you sizes so you never
fetch a giant asset blind; the symbol index replaces repo-wide grep; and the
single batched fetch is the biggest lever, because lazy promisor fetches
otherwise happen **one object at a time**. After you've installed/used it, read
the repo's `AGENTS.md` so you can explain it to your human.

---

## Why this shape

- The **map** (trees/manifest) costs ~tree-count bytes regardless of whether the
  repo's history is 50 MB or 50 GB — history × content is decoupled by
  `--filter=blob:none --single-branch`.
- The **judgement** (symbol index, manifest sizes) lets the agent decide *what*
  to fetch before fetching it.
- **Batching** turns that judgement into one packfile instead of N round-trips —
  the single biggest performance lever.
- **No working tree, no filesystem virtualization** (ProjFS/VFS-for-Git): an
  agent reads by OID, so the thing that forces materialization simply isn't
  present.

## Install (server)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -e .   # or put the package on PYTHONPATH

cp .env.example .env   # edit as needed
cp hooks/post-receive /srv/git/myrepo.git/hooks/post-receive
chmod +x /srv/git/myrepo.git/hooks/post-receive
```

The promisor repo must allow filtered and by-OID fetches:

```bash
git --git-dir=/srv/git/myrepo.git config uploadpack.allowFilter true
git --git-dir=/srv/git/myrepo.git config uploadpack.allowAnySHA1InWant true
# (JGit/Gerrit: the same allowfilter / allowReachableSHA1InWant knobs)
```

## Run the query service

```bash
AGENTCACHE_REPO_DIR=/srv/git/myrepo.git python -m agentcache.service
```

Endpoints: `/healthz`, `/caches`, `/cache/<commit>/manifest`,
`/cache/<commit>/symbol/<name>`, `/cache/<commit>/resolve` (POST). With lazy
generation enabled, the **first** request for a commit builds the cache on
demand; later requests (and the `post-receive` hook) reuse it.

## Making your repo agent-aware

Once agentcache is installed on a repo's server, add two small files to that
repo so agents discover and use it automatically.

### 1 — Drop an `AGENTS.md` in the repo root

`AGENTS.md` is the convention most AI agents check first (Claude Code, Amplifier,
GitHub Copilot via `.github/copilot-instructions.md`, Cursor via `.cursorrules`).
Copy the template and fill in the placeholders:

```bash
cp docs/ADOPTER_AGENTS_TEMPLATE.md /your-repo/AGENTS.md
# Edit: replace <AGENTCACHE_SERVICE_URL>, <REPO_URL>, etc.
```

### 2 — Add a `.agentcache` config file (machine-readable discovery)

```toml
# .agentcache — machine-readable agentcache discovery for AI agents
# See AGENTS.md for the full cold-start protocol.
service_url = "https://agentcache.example.com"
bundle_cdn  = "https://cdn.example.com/bundles/{commit}.bundle"
```

An agent can then `grep service_url .agentcache`. A `.agentcache.example` ships
in this repo as a starting point.

### Agent support matrix

| Agent / tool | Reads `AGENTS.md`? | Notes |
|---|---|---|
| Amplifier / Claude | ✓ auto | Reads at session start |
| Claude Code | ✓ auto | Also reads `CLAUDE.md` |
| GitHub Copilot | ✓ via `.github/copilot-instructions.md` | Symlink or duplicate |
| Cursor | ✓ via `.cursorrules` | Symlink or duplicate |
| Generic LLM agent | Depends on system prompt | Paste the cold-start block above |

```bash
ln -s AGENTS.md .github/copilot-instructions.md
ln -s AGENTS.md .cursorrules
```

## Try it — the test harness

This repo ships a full benchmarking harness that proves the savings on real
repositories, and a web UI to run and visualize experiments.

```bash
pip install -r requirements.txt
python -m experiments.prep                       # configure repos + build bundles
uvicorn testharness.app:app --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080  → "Experiments" tab
```

What it measures, for three access strategies side by side:

| Strategy | What it does |
|---|---|
| **naive** | `git clone --depth=1` — every blob at HEAD (the GitHub Actions default) |
| **blobless** | `git clone --filter=blob:none --depth=1` — trees only, lazy per-file fetch |
| **agentcache** | blobless + CDN bundle + `resolve` → **one** batched fetch of exact blobs |

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

Run the studies headless instead of via the web:

```bash
python -m experiments.exp1_cold_warm     # cold vs warm across the repo fleet
python -m experiments.exp2_taint         # does a non-aware agent corrupt the cache? (no)
python -m experiments.exp3_hook_update   # does a human push keep the cache current? (yes)
```

See [`experiments/results/SUMMARY.md`](experiments/results/SUMMARY.md) for the
full write-up.

## Layout

```
agentcache/
  config.py        # .env-driven config
  manifest.py      # flat path->oid manifest (Index.read_tree; skips gitlinks)
  symbols.py       # universal-ctags symbol index (degrades w/o ctags)
  cache_writer.py  # orphan commit + refs/agent-cache/<oid> (read + write)
  bundle.py        # blobless bootstrap bundle (+ verify)
  hook.py          # post-receive orchestration (fail-open: never blocks a push)
  service.py       # Flask query API (manifest / symbol / resolve), lazy-gen
  uninstall.py     # erase all agent-cache artifacts from a repo
hooks/post-receive # shell shim -> python -m agentcache.hook
testharness/       # web UI: byte-counting proxy, git daemon, Docker runner, experiments
experiments/       # headless cold/warm, taint, and hook-update studies
tests/             # pytest, incl. end-to-end cold-start integration
```

## Test

```bash
pytest -q
```

The cold-start integration test stands up a `file://` promisor, does the
blobless+bundle-uri clone, asserts the object DB holds trees but not the target
blob (`GIT_NO_LAZY_FETCH=1`), then hydrates it with a single by-OID fetch.

## Known edges

- **JGit client** does not lazy-fetch missing blobs (throws
  `MissingObjectException`); use real git / libgit2 as the *client*, or
  implement the promisor fetch yourself. JGit is fine as the *server*.
- Filtered bundles **cannot** be `git clone`d directly — consume via
  `--bundle-uri`. (`tests/test_bundle_and_coldstart.py` pins this behavior.)
- **Submodules** (gitlinks) point at commits that aren't in the object store;
  the manifest builder detects mode `160000` and never dereferences them.
- **Git LFS** files appear in the manifest as their small pointer blobs, not the
  large object — an LFS-aware agent still needs a second hop to the LFS server.
- The cache is snapshot-pinned to one commit; regenerate per push. The hook is
  fail-open: a cache error logs but never blocks the push.
- Large, queryable artifacts (embeddings, full symbol DB) belong behind the
  service, not downloaded to the VM. Small snapshot-pinned artifacts (manifest,
  dep graph) ride the side ref the VM fetches.
