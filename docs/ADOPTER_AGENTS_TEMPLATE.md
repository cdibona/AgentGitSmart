# Agent Instructions — `<REPO_NAME>`

## Getting full agent instructions (no clone needed)

If the agentgitsmart service is running, you can get commit-specific instructions
in a single HTTP request — no git clone required:

```bash
curl <AGENTGITSMART_SERVICE_URL>/agents.md
```

This returns the same document you are reading, but rendered with the actual
service URL, the current HEAD commit SHA, and copy-paste-ready shell commands.
The full instructions are stored in the orphaned cache commit — not in the
tracked file tree — so this works before you have cloned anything.

---

<!-- agentgitsmart-config
service_url: <AGENTGITSMART_SERVICE_URL>
bundle_cdn:  <BUNDLE_CDN_URL_OR_OMIT>
-->

> **This repo has agentgitsmart installed.**  Before you clone anything,
> read the cold-start protocol below.  On a large repo it can be the
> difference between a 30-second start and a 5-minute one, and saves
> the git server significant bandwidth per agent session.

---

## agentgitsmart cold-start protocol

This repository's git server runs agentgitsmart.  Instead of a full or
shallow clone, use this three-step sequence.  You only fetch the blobs
you are going to read.

```bash
# ── 0.  Set the commit you are working on ────────────────────────
COMMIT=$(git ls-remote <REPO_URL> refs/heads/<DEFAULT_BRANCH> | cut -f1)
# or, if you already have a clone: COMMIT=$(git rev-parse HEAD)

# ── 1.  Blobless clone — commits + trees, zero content bytes ─────
git clone \
  --filter=blob:none \
  --no-checkout \
  --branch <DEFAULT_BRANCH> \
  <REPO_URL> workspace
cd workspace

# Optional: if a bootstrap bundle CDN is configured, seed from it
# (the promisor then transfers almost nothing):
#   git clone --filter=blob:none --no-checkout \
#             --bundle-uri="<BUNDLE_CDN_URL>/${COMMIT}.bundle" \
#             --branch <DEFAULT_BRANCH> <REPO_URL> workspace

# ── 2.  Resolve — ask which OIDs you need, no content transferred ─
OIDS=$(curl -s -XPOST "<AGENTGITSMART_SERVICE_URL>/cache/${COMMIT}/resolve" \
       -H 'content-type: application/json' \
       -d '{"paths":["src/app.py","src/util.c"]}' \
     | jq -r '.fetch_oids[]')

# Check the response for "missing" paths you listed but don't exist:
#   jq '.missing' — those paths are not in this commit.

# ── 3.  Fetch — ONE batched packfile, only what you need ─────────
git fetch origin $OIDS

# Read blobs by OID (no working-tree checkout required):
git cat-file blob <OID>

# Or check out just the files you resolved:
git checkout HEAD -- src/app.py src/util.c
```

**Why this matters:** a naive `git clone --depth=1` materialises every
tracked file.  On a repo with 60 k files that is hundreds of megabytes
the agent never reads.  The protocol above fetches only the bytes it
will actually touch — usually a few kilobytes — in a single round trip.

---

## agentgitsmart service endpoints

Base URL: `<AGENTGITSMART_SERVICE_URL>`

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/healthz` | Liveness check — `{"status":"ok"}` |
| `GET`  | `/caches` | List commits that have a cache entry |
| `GET`  | `/cache/<commit>/manifest` | Full flat path→{oid,size,mode} for the commit |
| `GET`  | `/cache/<commit>/symbol/<name>` | All locations of a symbol + their OIDs |
| `POST` | `/cache/<commit>/resolve` | Resolve a list of paths → `{fetch_oids, missing, total_bytes}` |

### Symbol lookup (replaces grep-the-repo)

```bash
# Where is TokenRefresher defined, and what blob OID do I fetch?
curl -s "<AGENTGITSMART_SERVICE_URL>/cache/${COMMIT}/symbol/TokenRefresher" | jq .
# → {"name":"TokenRefresher","locations":[{"path":"src/app.py","line":5,"kind":"class","oid":"abc123...","size":1234}],"fetch_oids":["abc123..."]}

# Fetch that blob directly:
git fetch origin abc123...
git cat-file blob abc123...
```

This turns "grep the entire repo" (fetch all blobs, scan) into a lookup
that returns a handful of OIDs, which you fetch in one pack.  Symbols
are indexed server-side by universal-ctags at push time; delta re-indexing
means only changed files are re-scanned on each push
(`sudo apt-get install -y universal-ctags` to enable).

### Manifest (plan before fetching)

```bash
# Check a file's size before deciding whether to fetch it.
curl -s "<AGENTGITSMART_SERVICE_URL>/cache/${COMMIT}/manifest" \
  | jq '.entries[] | select(.path=="data/large-dataset.parquet") | .size'
```

The manifest carries sizes so you can skip files the agent will not
need or that are too large to be useful in a context window.

---

## Repository layout

```
<FILL IN REPO-SPECIFIC LAYOUT HERE>
```

---

## Build / test

```bash
<FILL IN BUILD AND TEST COMMANDS HERE>
```

---

## Working conventions

<FILL IN REPO-SPECIFIC CONVENTIONS HERE — branch naming, commit style, etc.>

---

## Things agents commonly get wrong in this repo

<FILL IN REPO-SPECIFIC GOTCHAS HERE>

---

*This file was generated from the
[agentgitsmart adopter template](https://github.com/cdibona/AgentGitSmart/blob/main/docs/ADOPTER_AGENTS_TEMPLATE.md).
See that repo for the full agentgitsmart documentation.*
