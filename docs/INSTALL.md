# Installation & adoption

How to install agentgitsmart on a Git server, run the query service, adopt it on
GitHub (where server hooks don't run), and make a repo agent-aware so agents
discover and use the cache automatically.

For *how* the pieces fit together, see [How AgentGitSmart works](HOW_IT_WORKS.md).
To decide whether you even need it, see [Blobless vs AgentGitSmart](BLOBLESS.md).

## Prerequisites & install (server)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -e .   # or put the package on PYTHONPATH

cp .env.example .env   # edit as needed
cp hooks/post-receive /srv/git/myrepo.git/hooks/post-receive
chmod +x /srv/git/myrepo.git/hooks/post-receive
```

Install [universal-ctags](https://github.com/universal-ctags/ctags) to enable the symbol index (highly recommended — without it the index is empty and every push triggers a full rebuild instead of a delta):

```bash
sudo apt-get install -y universal-ctags   # recommended: builds the symbol index; without it symbols are empty and every push rebuilds full
```

The promisor repo must allow filtered and by-OID fetches:

```bash
git --git-dir=/srv/git/myrepo.git config uploadpack.allowFilter true
git --git-dir=/srv/git/myrepo.git config uploadpack.allowAnySHA1InWant true
# (JGit/Gerrit: the same allowfilter / allowReachableSHA1InWant knobs)
```

## Run the query service

```bash
AGENTGITSMART_REPO_DIR=/srv/git/myrepo.git python -m agentgitsmart.service
```

Endpoints: `/healthz`, `/caches`, `/cache/<commit>/manifest`,
`/cache/<commit>/symbol/<name>`, `/cache/<commit>/resolve` (POST),
`/cache/<commit>/agents.md`, and `/agents.md` (the same document for the
most-recently cached commit). With lazy generation enabled, the **first**
request for a commit builds the cache on demand; later requests (and the
`post-receive` hook) reuse it.

`/cache/<commit>/manifest` returns the stored `manifest.json` verbatim — an
envelope whose file list is under `.entries[]`, each entry
`{path, oid, size, mode}`. It is not a flat path-keyed map.

## This repo dogfoods agentgitsmart

**(The GitHub adoption path — when server-side hooks aren't available.)**

AgentGitSmart uses agentgitsmart **on itself**. GitHub doesn't run server-side
`post-receive` hooks, so [`.github/workflows/agentgitsmart.yml`](../.github/workflows/agentgitsmart.yml)
does the equivalent in CI: on every push to `main` it runs
[`scripts/generate_agentgitsmart.py`](../scripts/generate_agentgitsmart.py), builds the
manifest + symbol index (+ a blobless bundle), and publishes
`refs/agent-git-smart/<commit>` back to the repo (plus a workflow artifact). The
[`.agentgitsmart`](../.agentgitsmart) file advertises this **service-less, side-ref
mode**, and [`AGENTS.md`](../AGENTS.md) gives agents the exact cold-start. So an
agent (or a GitHub Action) working on this repo can fetch only the files it
needs straight from the side ref — no running service required.

## Making your repo agent-aware

Once agentgitsmart is installed on a repo's server, add two small files to that
repo so agents discover and use it automatically.

### 1 — Drop an `AGENTS.md` in the repo root

`AGENTS.md` is the convention most AI agents check first (Claude Code, Amplifier,
GitHub Copilot via `.github/copilot-instructions.md`, Cursor via `.cursorrules`).
Copy the template and fill in the placeholders:

```bash
cp docs/ADOPTER_AGENTS_TEMPLATE.md /your-repo/AGENTS.md
# Edit: replace <AGENTGITSMART_SERVICE_URL>, <REPO_URL>, etc.
```

See [`ADOPTER_AGENTS_TEMPLATE.md`](ADOPTER_AGENTS_TEMPLATE.md) for the full
template.

### 2 — Add a `.agentgitsmart` config file (machine-readable discovery)

```toml
# .agentgitsmart — machine-readable agentgitsmart discovery for AI agents
# See AGENTS.md for the full cold-start protocol.
service_url = "https://agentgitsmart.example.com"
bundle_cdn  = "https://cdn.example.com/bundles/{commit}.bundle"
```

An agent can then `grep service_url .agentgitsmart`. A `.agentgitsmart.example` ships
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
