# How AgentGitSmart works (engineering)

The architecture, the delta symbol index, the loop-safety guarantee, why human
PRs never break the cache, the repo layout, and the known edges.

To install it, see [Installation & adoption](INSTALL.md). To decide whether you
need it at all, see [Blobless vs AgentGitSmart](BLOBLESS.md). To measure it, see
[Testing, results & the harness](TESTING.md).

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

## Symbol index: delta mode & load reporting

On every push, agentgitsmart re-ctags **only the changed files** and carries
forward unchanged symbols from the parent commit's cache
(`refs/agent-git-smart/<parent-sha>`), merging everything through
`canonicalize_symbols()` — a single deterministic chokepoint that guarantees the
**delta `symbols` payload is byte-identical to a full rebuild**, in identical key
order (only the envelope's wall-clock `generated_at` differs), preserving the
exp2 PRISTINE guarantee.

A full rebuild is used instead — and `fallback_reason` records why — when any
of the following apply:

- `AGENTGITSMART_DELTA_SYMBOLS=false`
- ctags is not installed (`ctags_unavailable`)
- root commit (no parents; full rebuild, `fallback_reason` stays `null` — note
  `generation.parent` is `null` on *every* full build, so read `fallback_reason`,
  not `parent`, to tell a root commit from a fallback)
- merge commit and `AGENTGITSMART_DELTA_ON_MERGE=false` (`merge_commit`)
- no parent cache exists (`parent_uncached`) or is unreadable (`parent_unreadable`)
- the parent was built without ctags (`parent_ctags_unavailable`)
- `GENERATOR_VERSION` mismatch — currently `0.2.0` (`version_mismatch`)
- `SYMBOLS_SCHEMA` mismatch — currently `2` (`schema_mismatch`)
- changed-file fraction exceeds `AGENTGITSMART_DELTA_MAX_RATIO` (`ratio_threshold`)

Every cache commit embeds a **`generation` block** in `meta.json` that serves
as the per-push load report:

```json
{
  "mode": "full",
  "parent": "<sha> | null",
  "files_in_tree": 1234,
  "files_changed": 12,
  "files_reindexed": 12,
  "files_carried_forward": 1222,
  "content_bytes_materialized": 48200,
  "symbol_count": 8901,
  "ctags_available": true,
  "fallback_reason": null
}
```

The same object is returned by `generate_for_commit()` for programmatic use.

Three env vars control delta behaviour (`1/true/yes/on` count as true):

| Variable | Default | Effect |
|---|---|---|
| `AGENTGITSMART_DELTA_SYMBOLS` | `true` | Enable delta re-indexing |
| `AGENTGITSMART_DELTA_ON_MERGE` | `true` | Allow delta on merge commits (first parent only) |
| `AGENTGITSMART_DELTA_MAX_RATIO` | *(unset)* | Fall back to full when changed/total exceeds this ratio |

## Loop safety

AgentGitSmart cannot trigger itself. Cache artifacts are written **in-process** via
`repo.references.create()`, so they never invoke `receive-pack` and never
re-fire the `post-receive` hook. The hook itself only acts on `refs/heads/*` and
**explicitly skips its own `ref_prefix`** (`refs/agent-git-smart/*`), so even a ref
update that did reach it would be ignored. On GitHub, the Action's
`push: branches: [main]` trigger excludes the `refs/agent-git-smart/*` artifact push
entirely — pushing a cache ref is not a push to `main`, so the workflow does not
re-run. This is a real, tested guarantee: see
[`agentgitsmart/hook.py`](../agentgitsmart/hook.py) and
[`tests/test_hook_ref_filter.py`](../tests/test_hook_ref_filter.py).

## How human PRs don't break the cache

A reasonable worry: *if agents rely on this cache, won't an ordinary teammate
who opens a PR — and has never heard of agentgitsmart — break it?*

**No. agentgitsmart is designed so that humans never have to know it exists.**

- **The cache is generated server-side, not client-side.** It's built by the
  `post-receive` hook (self-hosted) or a CI workflow (GitHub) when commits
  land. A contributor pushes or merges a PR exactly as they always have; the
  cache for the new commit is (re)built automatically. Nobody runs anything
  special, and there is nothing to forget.
- **It's keyed by immutable commit OID and lives in a side ref.** Each commit
  gets its own `refs/agent-git-smart/<oid>`, outside `refs/heads/*`. A new commit
  produces a *new* ref; it never edits an existing one. The cache for commit
  `A` is correct for `A` forever.
- **It's append-only and read-only to clients.** Agents *fetch* the side ref;
  they never push it. A non-agentgitsmart-aware agent (or human) doing a plain
  clone, edit, and push **cannot corrupt** the cache — there is no shared
  mutable state to corrupt. We prove this in
  [`experiments/exp2_taint`](../experiments/exp2_taint.py): after aware and
  *un*aware agents hammer the same repos, every cache ref is byte-identical
  (verdict: **PRISTINE** on all repos).
- **A human's merge keeps it current automatically.** When a PR merges to
  `main`, the hook/CI fires on the new HEAD and produces the cache for it. We
  prove this in [`experiments/exp3_hook_update`](../experiments/exp3_hook_update.py):
  a teammate with zero agentgitsmart awareness pushes, and the next agent finds an
  up-to-date cache (verdict: **PASS**).
- **The hook is fail-open.** If cache generation ever errors, it logs and
  returns success — it **never blocks a push or a merge**. Worst case, an agent
  finds no cache for one commit and falls back to a normal blobless fetch.

In short: humans do their normal git workflow; the cache follows along. The only
thing a maintainer does once is install the hook (or the CI workflow); after
that, PRs "just work."

## Repo layout

```
agentgitsmart/
  config.py        # .env-driven config
  manifest.py      # whole-tree manifest, .entries[] (Index.read_tree; skips gitlinks)
  symbols.py       # universal-ctags symbol index + delta re-indexing (SYMBOLS_SCHEMA=2; degrades w/o ctags; install: sudo apt-get install -y universal-ctags)
  cache_writer.py  # orphan commit + refs/agent-git-smart/<oid> (read + write)
  bundle.py        # blobless bootstrap bundle (+ verify)
  hook.py          # post-receive orchestration (fail-open: never blocks a push)
  service.py       # Flask query API (manifest / symbol / resolve), lazy-gen
  uninstall.py     # erase all agent-git-smart artifacts from a repo
hooks/post-receive # shell shim -> python -m agentgitsmart.hook
AGENTS.md          # agent instructions (tool-neutral)
CLAUDE.md          # thin pointer importing AGENTS.md (Claude Code reads this)
testharness/       # web UI: byte-counting proxy, git daemon, Docker runner, experiments
experiments/       # headless cold/warm, taint, and hook-update studies
tests/             # pytest, incl. end-to-end cold-start integration
```

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
