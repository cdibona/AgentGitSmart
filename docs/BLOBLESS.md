# Blobless clones vs AgentCache (and whether you need it)

There are three ways an agent can get a working copy of a big repo cheaply. Two
of them are stock Git and need nothing installed. AgentCache is the third — and
it only earns its keep on certain repos. This page is the honest comparison.

## Three strategies

| Strategy | What it does |
|---|---|
| **naive** | `git clone --depth=1` — every blob at HEAD (the GitHub Actions default) |
| **blobless** | `git clone --filter=blob:none` — full history + trees, but ZERO file content; blobs fetched lazily, one per file as it's read |
| **agentcache** | blobless + manifest/symbol `resolve` + **one** batched fetch of exactly the needed blobs + an optional CDN bootstrap bundle |

## You do NOT need to install AgentCache to use blobless

This is the key point, stated plainly:

**Blobless partial clone is a stock Git feature.** `git clone --filter=blob:none`
is built into Git itself. It works against GitHub and against any server that
enables `uploadpack.allowFilter` — which **GitHub already does**. No hook, no
query service, no side refs, no AGENTS.md — just a git flag.

For **many repos, blobless alone is already cheap.** If a repo is small or lean
(not many large blobs, not worked by many agents per commit), partial clone
already gets you most of the win, and the extra machinery of AgentCache buys you
little. That is exactly why AgentCache's own adoption tools
([`scripts/assess_repo.py`](../scripts/assess_repo.py) and the measured trial)
will tell you **"blobless is enough"** for small or lean repos — and abstain
rather than over-promise.

So: reach for blobless first. Reach for AgentCache only when the numbers say it
pays.

## What AgentCache adds on top of blobless

When a repo *is* blob-heavy and worked by many agents per commit, plain blobless
leaves real savings on the table, and AgentCache picks them up:

1. **The manifest gives file sizes**, so an agent never blind-fetches a huge
   asset — it can see a 2 GB blob coming and decide not to pull it.
2. **The symbol index replaces repo-wide grep.** A naive "grep the whole repo"
   under blobless lazily fetches *every* file; a symbol lookup returns a few OIDs
   instead.
3. **ONE batched fetch of exactly the needed blobs** instead of N lazy per-file
   round-trips. This is the single biggest lever, because lazy promisor fetches
   otherwise happen one object at a time.
4. **An optional CDN-cacheable bootstrap bundle** for cold starts, built once per
   commit and reused.

**The honest catch:** these only pay off when the repo is blob-heavy *and* worked
by many agents per commit. On a small or lean repo, blobless already wins and the
extra machinery is overhead. Don't guess — [measure with the harness](TESTING.md)
before adopting.

## See also

- [How AgentCache works](HOW_IT_WORKS.md) — the mechanism behind the manifest,
  symbol index, batched fetch, and bundle.
- [Testing, results & the harness](TESTING.md) — measure your own repo and get an
  honest "adopt / blobless is enough / inconclusive" verdict.
