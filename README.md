# agentcache

Server-side generation of per-commit *agent knowledge* so a disposable agent VM
can work on <1% of a huge repo without cloning its pack files.

On every push, a `post-receive` hook builds two artifacts for the pushed
commit and stores them as an **orphan commit** under
`refs/agent-cache/<source-commit-oid>` — out of the main history, behind the
same access control, cheap to fetch in isolation:

- **`manifest.json`** — flat `path → {oid, size, mode}` for the whole tree.
  Lets the agent plan which files to touch, and never fetch a giant asset by
  accident, *without fetching any content*.
- **`symbols.json`** — `symbol → [{path, line, kind}]` from universal-ctags.
  Turns "grep the repo" (fetch everything) into a lookup that returns a few
  OIDs.

It also (optionally) emits a **blobless bootstrap bundle** per branch tip for
CDN-cacheable cold starts.

A query service then answers `resolve(paths)` / `symbol(name)` with blob OIDs +
sizes, so the agent fetches only what it touches **in one batched fetch**.

## Why this shape

- The map (trees/manifest) costs ~tree-count bytes regardless of whether the
  repo's history is 50 MB or 50 GB — history × content is decoupled by
  `--filter=blob:none --depth=1 --single-branch`.
- The judgement (symbol index, manifest sizes) lets the agent decide *what* to
  fetch before fetching it.
- Batching turns that judgement into one packfile instead of N round-trips —
  the single biggest performance lever, because lazy fetches happen one object
  at a time otherwise.
- No working tree, no filesystem virtualization (ProjFS/VFS-for-Git): an agent
  reads by OID, so the thing that forces materialization simply isn't present.

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

## Agent cold start (in the disposable VM)

```bash
# 1. blobless clone, seeded by the CDN bundle (NOT a direct clone of the bundle)
git clone --filter=blob:none --no-checkout \
    --bundle-uri="https://cdn.example/bundles/$COMMIT.bundle" \
    "$PROMISOR_URL" repo
cd repo

# 2. plan against the side cache via the service — no content fetched
OIDS=$(curl -s -XPOST "$SVC/cache/$COMMIT/resolve" \
        -H 'content-type: application/json' \
        -d '{"paths":["src/a.cpp","src/a.h"]}' | jq -r '.fetch_oids[]')

# 3. ONE batched fetch of exactly the blobs the agent will read
git fetch origin $OIDS

# 4. ...edit, build over the subset, commit, push a thin pack, destroy the VM
```

## Layout

```
agentcache/
  config.py        # .env-driven config
  manifest.py      # flat path->oid manifest (Index.read_tree)
  symbols.py       # universal-ctags symbol index (degrades w/o ctags)
  cache_writer.py  # orphan commit + refs/agent-cache/<oid> (read + write)
  bundle.py        # blobless bootstrap bundle (+ verify)
  hook.py          # post-receive orchestration
  service.py       # Flask query API (manifest / symbol / resolve)
hooks/post-receive # shell shim -> python -m agentcache.hook
tests/             # pytest, incl. end-to-end cold-start integration
```

## Test

```bash
pytest -q
```

The cold-start integration test stands up a `file://` promisor, does the
blobless+bundle-uri clone, asserts the object DB holds trees but not the target
blob (`GIT_NO_LAZY_FETCH=1`), then hydrates it with a single by-OID fetch.

## Making your repo agent-aware

Once agentcache is installed on a repo's server, you need two small
additions to that repo so agents can discover and use it automatically.

### 1 — Drop an `AGENTS.md` in the repo root

`AGENTS.md` is the convention most AI agents check first (Claude,
Amplifier agents, GitHub Copilot with
`.github/copilot-instructions.md`, etc.).  Copy the template from this
repo and fill in the placeholders:

```bash
cp docs/ADOPTER_AGENTS_TEMPLATE.md /your-repo/AGENTS.md
# Edit: replace <AGENTCACHE_SERVICE_URL>, <REPO_URL>, etc.
```

The template gives agents the exact three-step cold-start protocol,
a service endpoint reference, a symbol-lookup example, and placeholders
for repo-specific layout and conventions.

### 2 — Add a `.agentcache` config file (machine-readable discovery)

Place this at the root of the repo so agents that look for structured
configuration can find the service URL without parsing prose:

```toml
# .agentcache — machine-readable agentcache discovery for AI agents
# See AGENTS.md for the full cold-start protocol.
service_url = "https://agentcache.example.com"
bundle_cdn  = "https://cdn.example.com/bundles/{commit}.bundle"
```

An agent can then do:
```bash
grep service_url .agentcache   # → service_url = "https://..."
```

A `.agentcache.example` is included in this repo as a starting point.

### What to tell agents that don't auto-read `AGENTS.md`

If your agent tool does not automatically read `AGENTS.md`, give it
this prompt at the start of the session:

> "This repository has agentcache installed.  The service is at
> `<AGENTCACHE_SERVICE_URL>`.  Do **not** do a full or shallow clone.
> Instead: (1) `git clone --filter=blob:none --no-checkout <REPO_URL>`,
> (2) POST `<AGENTCACHE_SERVICE_URL>/cache/<commit>/resolve` with a JSON
> body `{"paths":["the","files","you","need"]}` and get back
> `fetch_oids`, (3) `git fetch origin <oids>`.  Read blobs by OID with
> `git cat-file blob <oid>`.  This saves significant bandwidth and time
> on large repos."

Or the short form if your agent is context-aware:

> "Read `AGENTS.md` — this repo uses agentcache and has the cold-start
> protocol documented there."

### Agent support matrix

| Agent / tool | Reads `AGENTS.md`? | Notes |
|---|---|---|
| Amplifier / Claude (this tool) | ✓ auto | Reads at session start |
| Claude Code | ✓ auto | Also reads `CLAUDE.md` |
| GitHub Copilot Workspace | ✓ via `.github/copilot-instructions.md` | Symlink or duplicate |
| Cursor | ✓ via `.cursorrules` | Symlink or duplicate |
| Generic LLM agent | Depends on system prompt | Use the magic phrase above |

For tools that use a different filename, symlink the file:

```bash
ln -s AGENTS.md .github/copilot-instructions.md
ln -s AGENTS.md .cursorrules
```

## Known edges

- **JGit client** does not lazy-fetch missing blobs (throws
  `MissingObjectException`); use real git / libgit2 as the *client*, or
  implement the promisor fetch yourself. JGit is fine as the *server*.
- Filtered bundles **cannot** be `git clone`d directly — consume via
  `--bundle-uri`. (`tests/test_bundle_and_coldstart.py` pins this behavior.)
- The cache is snapshot-pinned to one commit; regenerate per push. The hook is
  fail-open: a cache error logs but never blocks the push.
- Large, queryable artifacts (embeddings, full symbol DB) belong behind the
  service, not downloaded to the VM. Small snapshot-pinned artifacts
  (manifest, dep graph) ride the side ref the VM fetches.
```
