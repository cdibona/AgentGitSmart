# agentgitsmart

> **⚠️ Proof of concept.** AgentGitSmart is an experimental prototype for exploring
> and measuring server-side per-commit agent caching — not production-hardened
> software. The included test harness is a *diagnostic* for finding where the
> approach helps and where it doesn't (on many repos the honest answer is
> "blobless is enough"). Treat results as evidence to reason about, not a
> promise. Expect rough edges.

**Let AI coding agents work on a huge repo while downloading less than 1% of it.**

agentgitsmart is server-side Git infrastructure. On every push, a `post-receive`
hook pre-computes per-commit *agent knowledge* and stores it as an **orphan
commit** under `refs/agent-git-smart/<source-commit-oid>` — out of the main history,
behind the same access control, cheap to fetch in isolation:

- **`manifest.json`** — every path in the tree, as
  `.entries[] → {path, oid, size, mode}`. Lets an agent plan which files to
  touch (and never fetch a 2 GB asset by accident) *without fetching any
  content*.
- **`symbols.json`** — `.symbols → {name: [{path, line, kind}]}` from
  universal-ctags. Turns "grep the whole repo" (fetch everything) into a lookup
  that returns a few OIDs.

Both files wrap their payload in a small envelope (`schema`,
`generator_version`, `generated_at`, `source_commit`), so read them as
`.entries[]` and `.symbols`, not as a bare top-level map.

It also (optionally) emits a [**blobless**](docs/BLOBLESS.md) bootstrap bundle
per branch tip for CDN-cacheable cold starts. A small query service answers
`resolve(paths)` / `symbol(name)` with blob OIDs + sizes, so the agent fetches
only what it touches **in one batched fetch** instead of N lazy round-trips.

The payoff, measured end-to-end (see [Try it](docs/TESTING.md)): an agent
editing 2% of a repo pulls **16×–9,562× less** over the network than a normal
`git clone`, while still getting full history.

---

## Getting started

### If you are human

**Is AgentGitSmart right for your repo?** Run this in your repository's root. It
measures your repo (naive vs blobless vs agentgitsmart) and **— if AgentGitSmart would
actually help — offers to set it up for you**. If it wouldn't, it says so and
stops:

```bash
curl -fsSL https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/scripts/try.sh | bash
```

> **⚠️ While this repo is private, the `curl` above returns 404** —
> `raw.githubusercontent.com` won't serve a private repo without a token. Until
> it goes public, clone AgentGitSmart once and run the same script directly against
> your repo (this does exactly what the one-liner would):
>
> ```bash
> # one-time: get the tooling (use SSH or a token since the repo is private)
> git clone https://github.com/cdibona/AgentGitSmart ~/AgentGitSmart
> # then, from YOUR repo's root, simulate the curl one-liner:
> bash ~/AgentGitSmart/scripts/try.sh "$PWD"
> ```
>
> (`try.sh` self-bootstraps a throwaway venv and measures the repo you point it
> at — here, your current directory.)

It's honest: on a small or lean repo it tells you **"blobless is enough"** and
won't push an install on you. When it *does* offer to install, it only scaffolds
files into your working tree for you to review and commit — it never pushes
anything on its own. For exactly what it measures, the lighter static predictor
(`assess_repo.py`), and the manual invocations, see
[Testing & results](docs/TESTING.md#try-it-on-your-repo).

Prefer to let your coding agent drive instead? Tell it (Claude Code, Codex CLI,
Amplifier, Gemini CLI, Cursor, …):

```
Read https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/README.md. Follow the instructions there.
```
*(If the repo already has an `AGENTS.md`, just say: "Read AGENTS.md — this repo
uses agentgitsmart.")*

### If you've been sent here by your human

You're an AI agent about to work on a repo that uses (or should use) agentgitsmart.
**Use this instead of a clone — a full or shallow `git clone` defeats the entire
point.** Two steps: set it up if needed, then work the repo.

**Step 1 — Is agentgitsmart already set up here?** Check the repo root:

```bash
grep -s service_url .agentgitsmart    # prints a URL → already set up, skip to Step 2
```

If it's already set up, go straight to Step 2. **If this is the first time**
(no `.agentgitsmart`, no `AGENTS.md`), set it up once — you only do this per project:

- **Self-hosted Git server** — install the hook + query service:
  ```bash
  cp hooks/post-receive /srv/git/myrepo.git/hooks/post-receive
  chmod +x /srv/git/myrepo.git/hooks/post-receive
  git --git-dir=/srv/git/myrepo.git config uploadpack.allowFilter true
  git --git-dir=/srv/git/myrepo.git config uploadpack.allowAnySHA1InWant true
  AGENTGITSMART_REPO_DIR=/srv/git/myrepo.git python -m agentgitsmart.service
  ```
- **GitHub** — server hooks don't run; use the CI workflow instead (see
  [This repo dogfoods agentgitsmart](docs/INSTALL.md#this-repo-dogfoods-agentgitsmart)).
- **Make it discoverable** so the next agent skips Step 1 — drop `AGENTS.md` and
  `.agentgitsmart` in the repo root (`cp docs/ADOPTER_AGENTS_TEMPLATE.md AGENTS.md`,
  then fill in the placeholders; see
  [Making your repo agent-aware](docs/INSTALL.md#making-your-repo-agent-aware)).

From then on every push keeps the cache current — including pushes from teammates
who've never heard of agentgitsmart (the hook runs server-side).

**Step 2 — Work the repo.** Cold-start instead of cloning, fetching only the
blobs you'll actually read:

```bash
# 0. Discover the service. An AgentGitSmart-enabled repo advertises it in one of:
#      .agentgitsmart        →  grep service_url .agentgitsmart
#      AGENTS.md          →  the cold-start protocol + service URL
#    SVC = the agentgitsmart service URL,  REPO_URL = the git remote,  COMMIT = HEAD

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

Finally, read the repo's `AGENTS.md` so you can explain agentgitsmart to your human.

---

## Dive deeper into AgentGitSmart

| Topic | Read |
|---|---|
| Install & adopt (server hook, GitHub Action, agent-aware setup) | [docs/INSTALL.md](docs/INSTALL.md) |
| How it works (architecture, delta indexing, loop-safety, guarantees, limits) | [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) |
| Blobless vs AgentGitSmart — and whether you even need it | [docs/BLOBLESS.md](docs/BLOBLESS.md) |
| Testing, results & the harness (measure your repo, experiment digest) | [docs/TESTING.md](docs/TESTING.md) |

Latest measured numbers from this installation:
[**`experiments/RECENT.md`**](experiments/RECENT.md) — a readable digest of the
most recent experiment runs.
