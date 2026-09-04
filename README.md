# AgentGitSmart

<p align="center">
  <em>Let AI coding agents work on a huge repo while downloading less than 1% of it.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License" /></a>
  <a href="https://github.com/cdibona/AgentGitSmart/releases"><img src="https://img.shields.io/github/v/tag/cdibona/AgentGitSmart?label=release&color=green" alt="Latest release" /></a>
  <a href="https://github.com/cdibona/AgentGitSmart/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs welcome" /></a>
  <a href="https://github.com/anthropics/claude-code"><img src="https://img.shields.io/badge/works%20with-Claude%20Code-6B4FBB?logo=anthropic&logoColor=white" alt="Works with Claude Code" /></a>
  <a href="https://github.com/openai/codex"><img src="https://img.shields.io/badge/works%20with-Codex%20CLI-412991?logo=openai&logoColor=white" alt="Works with Codex CLI" /></a>
</p>

---

> **Proof of concept.** An experimental prototype for measuring server-side
> per-commit agent caching — not production-hardened. On many repos the honest
> answer is "blobless is enough", and the tooling here will tell you so.

## Getting started

### If you are human

Tell your favorite coding agent (Claude Code, Codex CLI, opencode, Cursor, Gemini CLI):

```
Read https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/README.md and follow
the "If you've been sent here by your human" section. Measure this repo first, show me
the verdict, and only set it up if the measurement says it's worth it.
```

Or measure it yourself, from your repo's root:

```bash
curl -fsSL https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/scripts/try.sh | bash
```

Temp dirs only, deleted on exit. If agentgitsmart wouldn't help, it says
**"blobless is enough"** and stops. If it would, it offers to scaffold the setup
files for you to review — it never runs `git add`, `commit`, or `push`.

### If you've been sent here by your human

**Step 0 — already set up?** `cat .agentgitsmart`. If it exists, skip to Step 2 and
note the mode: `side-ref` (service-less, read the cache from a git ref — the GitHub
path, and what this repo uses) or a non-empty `service_url` (a query service is
running). Otherwise continue.

**Step 1 — measure before you adopt. Do not skip this.** Installing this where it
doesn't pay is a real cost to the humans maintaining the repo.

```bash
curl -fsSL https://raw.githubusercontent.com/cdibona/AgentGitSmart/main/scripts/try.sh \
  | bash -s -- "$PWD" --json
```

Report the `verdict` to your human before doing anything else:

| Verdict | What you do |
|---|---|
| `blobless is enough` | **Stop. Do not install.** Stock `git clone --filter=blob:none` already wins here. This is a good outcome. |
| `AgentGitSmartBlobless` | **Don't install the server.** Recommend blobless + one batched fetch of OIDs read from the local trees. |
| `agentgitsmart worthwhile` | Adopt: re-run without `--json` and answer the prompt. |

Adoption creates four files — `.agentgitsmart` (discovery), `AGENTS.md`
(cross-tool instructions), `CLAUDE.md` (imports `AGENTS.md`), and, on GitHub,
`.github/workflows/agentgitsmart.yml` (server hooks don't run there, so CI builds
the cache). It creates, never overwrites. **Leave them uncommitted for your human
to review.** Self-hosted servers install a `post-receive` hook instead — see
[docs/INSTALL.md](docs/INSTALL.md).

**Step 2 — work the repo.** A full or shallow `git clone` defeats the entire point.

```bash
# Service-less (mode = "side-ref"). Needs nothing but git.
git clone --filter=blob:none --no-checkout "$REPO_URL" repo && cd repo   # zero file content
C=$(git rev-parse HEAD)

git fetch origin "refs/agent-git-smart/$C:refs/agent-git-smart/$C"
git cat-file -p "refs/agent-git-smart/$C:manifest.json"   # .entries[] -> {path,oid,size,mode}
git cat-file -p "refs/agent-git-smart/$C:symbols.json"    # .symbols -> {name: [{path,line,kind}]}

git fetch origin <oid> <oid> ...    # ONE batched fetch of only what you need
git cat-file blob <oid>             # read by OID, no checkout
```

With a query service, ask it instead of reading the ref — `POST $SVC/cache/$C/resolve`
with the paths you want returns `fetch_oids`, and `--bundle-uri="$SVC/bundles/$C.bundle"`
seeds history from a CDN. Full protocol in [AGENTS.md](AGENTS.md).

No cache ref for this commit? Nothing breaks — you still have a blobless clone.

## How it works

On every push a `post-receive` hook (or, on GitHub, a CI workflow) pre-computes
per-commit *agent knowledge* and stores it as an **orphan commit** under
`refs/agent-git-smart/<commit-oid>` — out of normal history, behind the same access
control, cheap to fetch alone:

- **`manifest.json`** — every path with its OID, size and mode, so an agent can plan
  what to touch, and never fetch a 2 GB asset by accident, *without fetching content*.
- **`symbols.json`** — a universal-ctags index, turning "grep the whole repo" (which
  under blobless fetches everything) into a lookup returning a few OIDs.

The biggest lever is batching: lazy promisor fetches otherwise happen one object at
a time. Humans never have to know it exists — the cache is built server-side, keyed
by immutable commit OID, append-only, and the hook is fail-open, so a teammate who
has never heard of agentgitsmart can't break it.

Measured end-to-end, an agent editing 2% of a repo pulls **16×–9,562× less** over the
network than a normal `git clone`, while still getting full history:

| Repo | naive | blobless | **agentgitsmart** | vs naive |
|------|------:|---------:|---------------:|---------:|
| anthropic-cookbook | 153 MiB | 45 KiB | **16 KiB** | 9,562× |
| ohmyzsh | 3 MiB | 60 KiB | **5 KiB** | 645× |
| cpython | 43 MiB | 562 KiB | **349 KiB** | 126× |
| codex | 10 MiB | 877 KiB | **659 KiB** | 16× |

Warm steady state, 15-repo fleet, measured through a byte-counting proxy. The first
(cold) visit pays full cost before dropping to these numbers.

## Docs

| Topic | Read |
|---|---|
| Install & adopt (server hook, GitHub Action, agent-aware setup) | [docs/INSTALL.md](docs/INSTALL.md) |
| Architecture, delta indexing, loop safety, guarantees, limits | [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) |
| Blobless vs AgentGitSmart — and whether you even need it | [docs/BLOBLESS.md](docs/BLOBLESS.md) |
| Testing, results & the harness | [docs/TESTING.md](docs/TESTING.md) |
| Latest measured numbers from this installation | [experiments/RECENT.md](experiments/RECENT.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
