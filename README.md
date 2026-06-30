# agentcache

> **⚠️ Proof of concept.** AgentCache is an experimental prototype for exploring
> and measuring server-side per-commit agent caching — not production-hardened
> software. The included test harness is a *diagnostic* for finding where the
> approach helps and where it doesn't (on many repos the honest answer is
> "blobless is enough"). Treat results as evidence to reason about, not a
> promise. Expect rough edges.

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

It also (optionally) emits a [**blobless**](docs/BLOBLESS.md) bootstrap bundle
per branch tip for CDN-cacheable cold starts. A small query service answers
`resolve(paths)` / `symbol(name)` with blob OIDs + sizes, so the agent fetches
only what it touches **in one batched fetch** instead of N lazy round-trips.

The payoff, measured end-to-end (see [Try it](docs/TESTING.md)): an agent
editing 2% of a repo pulls **17×–10,000× less** over the network than a normal
`git clone`, while still getting full history.

---

## Getting started

### If you are human

Tell your favorite coding agent (Claude Code, Codex CLI, Amplifier, Gemini CLI,
Cursor, …):

```
Read https://raw.githubusercontent.com/cdibona/AgentCache/main/README.md. Follow the instructions there.
```

That's the whole job. Your agent does the rest — adopting agentcache on your Git
server and making the repo agent-aware — by following the section below. *(If the
repo already has an `AGENTS.md`, just say: "Read AGENTS.md — this repo uses
agentcache.")*

**Not sure it's worth adopting?** Point the analyzer at a local clone (or a
GitHub URL) for a quick, honest read on whether agentcache would help *your*
repo:

```bash
python scripts/assess_repo.py /path/to/your-repo        # or a https://github.com/... URL
```

It reports `agentcache worthwhile`, `blobless is enough` (your repo is too small
or too lean to benefit), or `inconclusive — measure to be sure`. This is a
**static prediction from repo shape, not a measured result** — it is deliberately
conservative and **abstains rather than over-promise** (a false "adopt" wastes
your time).

**Want real measured numbers?** From your repo's root:
`curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | bash`
— see [Testing & results](docs/TESTING.md#try-it-on-your-repo) for what it
measures.

### If you've been sent here by your human

You're an AI agent about to work on a repo that uses (or should use) agentcache.
**Use this instead of a clone — a full or shallow `git clone` defeats the entire
point.** Two steps: set it up if needed, then work the repo.

**Step 1 — Is agentcache already set up here?** Check the repo root:

```bash
grep -s service_url .agentcache    # prints a URL → already set up, skip to Step 2
```

If it's already set up, go straight to Step 2. **If this is the first time**
(no `.agentcache`, no `AGENTS.md`), set it up once — you only do this per project:

- **Self-hosted Git server** — install the hook + query service:
  ```bash
  cp hooks/post-receive /srv/git/myrepo.git/hooks/post-receive
  chmod +x /srv/git/myrepo.git/hooks/post-receive
  git --git-dir=/srv/git/myrepo.git config uploadpack.allowFilter true
  git --git-dir=/srv/git/myrepo.git config uploadpack.allowAnySHA1InWant true
  AGENTCACHE_REPO_DIR=/srv/git/myrepo.git python -m agentcache.service
  ```
- **GitHub** — server hooks don't run; use the CI workflow instead (see
  [This repo dogfoods agentcache](docs/INSTALL.md#this-repo-dogfoods-agentcache)).
- **Make it discoverable** so the next agent skips Step 1 — drop `AGENTS.md` and
  `.agentcache` in the repo root (`cp docs/ADOPTER_AGENTS_TEMPLATE.md AGENTS.md`,
  then fill in the placeholders; see
  [Making your repo agent-aware](docs/INSTALL.md#making-your-repo-agent-aware)).

From then on every push keeps the cache current — including pushes from teammates
who've never heard of agentcache (the hook runs server-side).

**Step 2 — Work the repo.** Cold-start instead of cloning, fetching only the
blobs you'll actually read:

```bash
# 0. Discover the service. A AgentCache-enabled repo advertises it in one of:
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
otherwise happen **one object at a time**.

Finally, read the repo's `AGENTS.md` so you can explain agentcache to your human.

---

## Dive deeper into AgentCache

| Topic | Read |
|---|---|
| Install & adopt (server hook, GitHub Action, agent-aware setup) | [docs/INSTALL.md](docs/INSTALL.md) |
| How it works (architecture, delta indexing, loop-safety, guarantees, limits) | [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) |
| Blobless vs AgentCache — and whether you even need it | [docs/BLOBLESS.md](docs/BLOBLESS.md) |
| Testing, results & the harness (measure your repo, experiment digest) | [docs/TESTING.md](docs/TESTING.md) |

Latest measured numbers from this installation:
[**`experiments/RECENT.md`**](experiments/RECENT.md) — a readable digest of the
most recent experiment runs.
