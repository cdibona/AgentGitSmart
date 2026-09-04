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

It touches nothing: the script clones the tooling into a temp dir, builds a
throwaway venv, measures, and deletes itself on exit. It's also honest — on a
small or lean repo it tells you **"blobless is enough"** and won't push an
install on you. When it *does* offer to install, it only scaffolds files into
your working tree for you to review and commit; it never runs `git add`,
`commit`, or `push`, and never touches a remote. For exactly what it measures,
the lighter static predictor (`assess_repo.py`), and the manual invocations, see
[Testing & results](docs/TESTING.md#try-it-on-your-repo).

#### Or tell Claude, Codex, or opencode to do it

Prefer to let your coding agent drive? Paste this into Claude Code, Codex CLI,
opencode, Cursor, Gemini CLI, Amplifier, or anything else that can run shell
commands:

```
This repo may or may not benefit from AgentGitSmart — server-side Git caching that
lets coding agents work on a big repo without downloading all of it.

Read https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/README.md and
follow its "If you've been sent here by your human" section, in order:

1. MEASURE this repo first and show me the verdict. Do not skip this.
2. If the verdict is "blobless is enough", stop and tell me so — that is a
   perfectly good outcome and means we should not adopt it.
3. Only if the verdict says agentgitsmart is worthwhile, set it up: scaffold the
   adoption files, explain what each one does, and leave them uncommitted for
   me to review. Do not push anything.
```

The measure-first ordering is the point — it stops an eager agent installing
infrastructure this repo doesn't need. If the repo is **already** set up, the
whole prompt collapses to: *"Read AGENTS.md — this repo uses agentgitsmart."*

### If you've been sent here by your human

You're an AI agent about to work on a repo that uses — or is considering —
agentgitsmart. Three steps: **check**, then **measure and adopt** if it isn't set
up, then **work the repo**.

**Step 0 — Is it already set up here?** Check the repo root:

```bash
cat .agentgitsmart      # exists → already set up; note `mode` and `service_url`
```

If that file exists, skip straight to **Step 2** — and note which mode it
declares, because the two are read differently:

- `mode = "side-ref"` with an empty `service_url` → **service-less**. Read the
  cache straight out of a git ref. This is what the GitHub adoption path
  produces, and what this repo itself uses.
- a non-empty `service_url` → a **query service** is running. Ask it.

If there's no `.agentgitsmart`, continue to Step 1.

---

**Step 1 — Measure before you adopt. Do not skip this.**

agentgitsmart is not right for every repo, and installing it where it doesn't pay
is a real cost to the humans who maintain this codebase. Measure first:

```bash
# Measured trial: mirrors the repo, stands up a byte-counting proxy, and
# reports actual network bytes for each strategy. Temp dirs only; deleted on exit.
curl -fsSL https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/scripts/try.sh \
  | bash -s -- "$PWD" --json
```

Read the `verdict` field and **report it to your human before doing anything
else**:

| Verdict | What you do |
|---|---|
| `blobless is enough` | **Stop. Do not install.** Tell them stock `git clone --filter=blob:none` already gets the win here. This is a good outcome, not a failure. |
| `AgentGitSmartBlobless` | **Do not install the server.** Recommend blobless + one batched fetch of OIDs read from the local trees — no hook, no service, no side ref. |
| `agentgitsmart worthwhile` | Proceed to adoption below. |

**Adoption (only on `agentgitsmart worthwhile`).** Re-run without `--json` and
answer the prompt, and the trial scaffolds the files for you. It *creates*, never
overwrites, and never touches git or a remote:

| File | Purpose |
|---|---|
| `.agentgitsmart` | Machine-readable discovery — how the next agent finds the cache |
| `AGENTS.md` | The cross-tool convention: Codex CLI, opencode, Cursor, Amplifier |
| `CLAUDE.md` | What Claude Code reads; imports `AGENTS.md` so there's one source of truth |
| `.github/workflows/agentgitsmart.yml` | GitHub only — server hooks don't run there, so CI builds the cache on every push to the default branch |

Then tell your human what changed and **leave it uncommitted for them to
review**. Do not commit or push adoption files on your own.

On a **self-hosted** Git server there is no CI step — install the hook and the
query service on the server instead:

```bash
cp hooks/post-receive /srv/git/myrepo.git/hooks/post-receive
chmod +x /srv/git/myrepo.git/hooks/post-receive
git --git-dir=/srv/git/myrepo.git config uploadpack.allowFilter true
git --git-dir=/srv/git/myrepo.git config uploadpack.allowAnySHA1InWant true
AGENTGITSMART_REPO_DIR=/srv/git/myrepo.git python -m agentgitsmart.service
```

From then on every push keeps the cache current — including pushes from teammates
who've never heard of agentgitsmart, because it runs server-side. See
[docs/INSTALL.md](docs/INSTALL.md) for the full adoption guide.

---

**Step 2 — Work the repo.** Now use the cache. **A full or shallow `git clone`
defeats the entire point** — cold-start instead, fetching only the blobs you'll
actually read.

**Service-less (`mode = "side-ref"`) — no service, just git.** This is the
GitHub path and the one to try first; it needs nothing but `git`:

```bash
# 1. Blobless clone: full history + every tree, ZERO file content.
git clone --filter=blob:none --no-checkout "$REPO_URL" repo && cd repo
C=$(git rev-parse HEAD)

# 2. Fetch this commit's cache ref and read the pre-built index. No content yet.
git fetch origin "refs/agent-git-smart/$C:refs/agent-git-smart/$C"
git cat-file -p "refs/agent-git-smart/$C:manifest.json"   # .entries[] -> {path,oid,size,mode}
git cat-file -p "refs/agent-git-smart/$C:symbols.json"    # .symbols -> {name: [{path,line,kind}]}

# 3. Pick the OIDs you need out of .entries[] (sizes are right there, so you can
#    see a 2 GB asset coming), then ONE batched fetch — not one per file.
git fetch origin <oid> <oid> ...

# 4. Read by OID, no checkout needed.
git cat-file blob <oid>
```

If a commit has no cache ref yet, nothing breaks: you still have a blobless
clone, so fall back to reading files normally and carry on.

**With a query service (non-empty `service_url`)** — the service resolves paths
and symbols for you, and can seed history from a CDN bundle:

```bash
# 0. SVC = service_url from .agentgitsmart, REPO_URL = the git remote, COMMIT = HEAD

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

Finally, read the repo's `AGENTS.md` (or `CLAUDE.md`, which imports it) so you
can explain agentgitsmart to your human.

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
