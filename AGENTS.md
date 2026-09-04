# Agent Instructions — AgentGitSmart / agentgitsmart

## This repo is agentgitsmart-enabled (service-less / side-ref mode)

> **If you are an agent about to work on this repo, do NOT clone all of it.**
> This repository publishes its own agent-knowledge cache on every push to
> `main` (via `.github/workflows/agentgitsmart.yml`) as a git side ref. There is
> no hosted query service — read the cache straight from the ref:
>
> ```bash
> # blobless clone: full history + trees, ZERO file content
> git clone --filter=blob:none --no-checkout <REPO_URL> repo && cd repo
> C=$(git rev-parse HEAD)
>
> # fetch the side ref for this commit and read the manifest/symbols
> git fetch origin "refs/agent-git-smart/$C:refs/agent-git-smart/$C"
> git cat-file -p "refs/agent-git-smart/$C:manifest.json"   # .entries[] -> {path,oid,size,mode}
> git cat-file -p "refs/agent-git-smart/$C:symbols.json"    # .symbols -> {name: [{path,line,kind}]}
>
> # fetch ONLY the blobs you'll actually read, then read them by OID
> git fetch origin <oid> <oid> ...
> git cat-file blob <oid>
> ```
>
> See the README "If you've been sent here by your human" section and `.agentgitsmart`
> for the machine-readable discovery file.
>
> **`CLAUDE.md` in the repo root imports this file** (Claude Code reads
> `CLAUDE.md`, not `AGENTS.md`).  Keep instructions here, not there, so every
> agent reads the same thing.

---

> **This file is for AI agents working on the AgentGitSmart codebase.**
> If you are an agent working on a *different* repo that happens to use
> agentgitsmart as its server-side cache, see that repo's own `AGENTS.md`
> instead — it will tell you how to do an efficient cold start there.

---

## What this repository is

**agentgitsmart** is a server-side Git infrastructure tool.  It runs as a
`post-receive` hook that pre-computes per-commit knowledge artifacts and
stores them as orphan commits under `refs/agent-git-smart/<commit-oid>`.  A
Flask query service lets agent VMs fetch only the blobs they will
actually touch, in a single batched `git fetch`, instead of cloning
everything.

## Package layout

```
agentgitsmart/           Python package (the server-side tool)
  __init__.py         GENERATOR_VERSION constant — bump on schema changes
  config.py           AgentGitSmartConfig frozen dataclass, all AGENTGITSMART_* env vars
  manifest.py         build_manifest()  — {schema,...,entries:[{path,oid,size,mode}]} via Index.read_tree
  symbols.py          build_symbol_index() / build_symbol_index_delta() — universal-ctags JSON;
                       delta re-ctags only changed files, carries forward unchanged symbols
                       (SYMBOLS_SCHEMA=2); degrades gracefully without ctags
  cache_writer.py     write_cache() / read_artifact() — orphan commit storage
  bundle.py           create_blobless_bundle() / verify_bundle()
  hook.py             post-receive orchestration; main() is fail-open (never blocks push);
                       generate_for_commit() returns a result dict that includes the generation block
  service.py          Flask app: /healthz /caches /cache/<c>/manifest /cache/<c>/symbol/<n>
                       /cache/<c>/resolve (POST) /cache/<c>/agents.md /agents.md
  generate.py         CLI one-shot generation for a commit (used by the GitHub Action)
  uninstall.py        Erase all agent-git-smart artifacts from a repo

hooks/post-receive    Shell shim: exec python3 -m agentgitsmart.hook

AGENTS.md             This file — instructions for every agent tool
CLAUDE.md             Thin pointer that imports AGENTS.md (Claude Code reads it)
docs/ADOPTER_AGENTS_TEMPLATE.md   AGENTS.md template scaffolded into adopter repos
docs/ADOPTER_CLAUDE_TEMPLATE.md   CLAUDE.md template scaffolded into adopter repos

tests/
  conftest.py         repo + cfg fixtures; FILES dict (TokenRefresher, make_refresher, str_len)
  test_manifest.py    completeness, sort order, OID/size accuracy
  test_symbols.py     ctags degradation + symbol indexing (skipped if ctags absent)
  test_cache_writer.py orphan commit, side ref, readback, idempotency
  test_service.py     Flask endpoints, resolve returns correct fetch_oids
  test_bundle_and_coldstart.py  end-to-end: blobless clone → verify blobs absent → targeted fetch
  (…plus test_delta_symbols, test_lazy_and_uninstall, test_taint_fallback,
   test_try_agentgitsmart, test_assess_repo, test_experiments, and more —
   18 files in total)

benchmark/            Stand-alone benchmarking scripts (not part of the installed package)
  approaches/         naive.py / blobless.py / blobless_batch.py / agentgitsmart.py
                       (run.py wires up three; blobless_batch is driven by testharness/)
  run.py              --smoke mode requires no setup; full mode requires a local repo
  setup_repo.sh       Mirror a local repo into benchmark/repos/, install hook, generate cache

testharness/          Local web-based test harness (FastAPI + Alpine.js + Chart.js)
  start.sh            One-command startup: git daemon + proxy + agentgitsmart svc + web UI
  app.py              FastAPI routes, lifespan manages all subprocesses
  proxy.py            Transparent TCP byte-counting proxy (port 9419 → 9418)
  runner.py           Orchestrates the three approaches; emits SSE events
```

## Delta symbol indexing & load reporting

On every push `build_symbol_index_delta()` re-ctags **only changed files** and
carries forward unchanged symbols from the parent cache
(`refs/agent-git-smart/<parent-sha>`).  Everything is merged through
`canonicalize_symbols()` — the single deterministic chokepoint — so the delta
`symbols` payload is **byte-identical to a full rebuild**, in identical key
order.  (Only the envelope's wall-clock `generated_at` differs between runs.)

### Fallback ladder

When delta is not possible the generation records a `fallback_reason`:

| Reason | Cause |
|---|---|
| `delta_disabled` | `AGENTGITSMART_DELTA_SYMBOLS=false` |
| `ctags_unavailable` | ctags binary not found |
| *(none — root commit)* | No parents; full rebuild, `fallback_reason` stays `null` |
| `merge_commit` | Merge commit and `AGENTGITSMART_DELTA_ON_MERGE=false` |
| `parent_uncached` | Parent has no cache entry |
| `parent_unreadable` | Parent cache exists but cannot be read |
| `schema_mismatch` | Parent `SYMBOLS_SCHEMA` ≠ current (`2`) |
| `version_mismatch` | Parent `GENERATOR_VERSION` ≠ current (`0.2.0`) |
| `parent_ctags_unavailable` | Parent index was built without ctags |
| `ratio_threshold` | Changed/total files > `AGENTGITSMART_DELTA_MAX_RATIO` |

Note: `generation.parent` is `null` on **every** full build, not only on root
commits — a full build has no delta base to record.  Read `fallback_reason` (not
`parent`) to tell a root commit (`null`) from a fallback.

### Generation block (meta.json + generate_for_commit() return value)

```json
{
  "mode": "full | delta",
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

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

Install `universal-ctags` to enable the full test suite (roughly a dozen
symbol/delta tests are skipped without it):

```bash
sudo apt-get install -y universal-ctags
pytest -q                          # tests/       — 272 passed (with ctags)
pytest testharness/tests -q        # harness      — 34 passed, 3 skipped (psutil absent)
```

The ctags-gated tests are the two in `tests/test_symbols.py` and the
`_SKIP_NO_CTAGS` group in `tests/test_delta_symbols.py`; the harness skips
three metrics tests when `psutil` is not installed.  All tests are
self-contained and create ephemeral bare repos in `tmp_path`.

## Running the benchmark smoke test

```bash
python -m benchmark.run --smoke
```

No additional setup required.  Spins up an in-process fixture repo,
generates cache, starts the Flask service, and compares all three
approaches.

## Running the test harness

```bash
bash testharness/start.sh
# → http://127.0.0.1:8080
```

The host is configurable via `AGENTGITSMART_WEB_HOST` (default `127.0.0.1`); set
it to a routable address to reach the harness from another machine.  See
[docs/TESTING.md](docs/TESTING.md#serving-the-harness-beyond-localhost).

Requires repos in `benchmark/repos/`.  See `benchmark/README.md`.

## Key design invariants — do not break these

| Invariant | Why |
|-----------|-----|
| `hook.main()` is **fail-open** — exceptions log but never exit non-zero | A cache failure must never block a push |
| Cache commits have **no parents** (`parents=[]`) | Orphan commits never appear in normal history walks or `git log` |
| Artifact names must be **flat** (no `/`) | `write_cache()` enforces this; tree depth is always 1 |
| `build_manifest()` uses `Index.read_tree()` | Do not replace with hand-rolled recursion — Index handles submodule entries correctly |
| `GENERATOR_VERSION` is embedded in every artifact | Bump it if the artifact schema changes so stale caches can be detected. Current value: `0.2.0`; `SYMBOLS_SCHEMA=2` — bump both on artifact schema changes |
| Delta mode must produce a **byte-identical** `symbols` payload to a full rebuild (envelope `generated_at` aside) | `canonicalize_symbols()` is the single chokepoint — all symbol merges go through it |

## Configuration knobs

All env vars have an `AGENTGITSMART_` prefix.  Boolean vars accept `1/true/yes/on`
(case-insensitive) as `true`.

| Variable | Default | Scope | Effect |
|---|---|---|---|
| `AGENTGITSMART_REPO_DIR` | `$GIT_DIR` | core | Path to the bare repo; required when not running inside the hook |
| `AGENTGITSMART_REF_PREFIX` | `refs/agent-git-smart` | core | Namespace for cache side-refs |
| `AGENTGITSMART_CTAGS_BIN` | `ctags` | core | ctags executable; set to the full path if not on `$PATH` |
| `AGENTGITSMART_BUNDLE_DIR` | *(unset)* | core | Directory for blobless bootstrap bundles; leave unset to disable |
| `AGENTGITSMART_BUNDLE_FILTER` | `blob:none` | core | git bundle filter string |
| `AGENTGITSMART_BOT_NAME` | `AgentGitSmart Bot` | core | Author name stamped on orphan cache commits |
| `AGENTGITSMART_BOT_EMAIL` | `agentgitsmart@localhost` | core | Author email stamped on orphan cache commits |
| `AGENTGITSMART_SERVICE_HOST` | `127.0.0.1` | service | Query service bind host |
| `AGENTGITSMART_SERVICE_PORT` | `8765` | service | Query service bind port |
| `AGENTGITSMART_SERVICE_URL` | *(empty)* | service | Public URL embedded in artifacts; leave blank to omit |
| `AGENTGITSMART_LAZY_GENERATION` | `true` | service | Build cache on first HTTP request if the hook did not run |
| `AGENTGITSMART_DELTA_SYMBOLS` | `true` | delta | Enable delta re-indexing |
| `AGENTGITSMART_DELTA_ON_MERGE` | `true` | delta | Allow delta on merge commits (first parent only) |
| `AGENTGITSMART_DELTA_MAX_RATIO` | *(unset)* | delta | Fall back to full when `changed/total > ratio`; unset = no cap |
| `AGENTGITSMART_REF` | *(output only)* | scripts | Not read as input — `generate.py` *emits* `::AGENTGITSMART_REF::<ref>` for the GitHub Action to capture |
| `AGENTGITSMART_WEB_HOST` | `127.0.0.1` | **testharness only** | Bind host for the FastAPI test-harness UI |
| `AGENTGITSMART_WEB_PORT` | `8080` | **testharness only** | Bind port for the FastAPI test-harness UI |
| `AGENTGITSMART_GIT_PORT` | `9418` | **testharness only** | Upstream `git daemon` port |
| `AGENTGITSMART_PROXY_PORT` | `9419` | **testharness only** | Byte-counting proxy port |
| `AGENTGITSMART_SVC_PORT` | `8765` | **testharness only** | Query service port the harness starts |
| `AGENTGITSMART_SERVICE_LOG` | *(unset)* | **testharness only** | File the harness writes service logs to |

## Coding conventions

- All source files use `from __future__ import annotations`.
- Public functions have docstrings.
- Tests use `tmp_path`; never write to the working tree.
- The Flask service (`service.py`) must remain importable without a
  running repo — `create_app(cfg)` is the only entry point.
- Do not add runtime dependencies beyond `requirements.txt` without
  discussion.  The install footprint is intentionally tiny.
