# Agent Instructions — PackCache / agentcache

## agentcache artifact (if you're in a PackCache-enhanced repo)

> **You are reading the tracked stub.** If this is a PackCache-enabled repo
> and the service is running, you can get richer, commit-specific instructions
> without any clone:
>
> ```bash
> curl <AGENTCACHE_SERVICE_URL>/agents.md
> ```
>
> The full instructions are stored as an artifact in the orphaned cache commit
> and served by the agentcache query service.  The tracked AGENTS.md is just
> a pointer; the artifact has the actual service URL, exact commit, and
> copy-paste shell snippets.

---

> **This file is for AI agents working on the PackCache codebase.**
> If you are an agent working on a *different* repo that happens to use
> agentcache as its server-side cache, see that repo's own `AGENTS.md`
> instead — it will tell you how to do an efficient cold start there.

---

## What this repository is

**agentcache** is a server-side Git infrastructure tool.  It runs as a
`post-receive` hook that pre-computes per-commit knowledge artifacts and
stores them as orphan commits under `refs/agent-cache/<commit-oid>`.  A
Flask query service lets agent VMs fetch only the blobs they will
actually touch, in a single batched `git fetch`, instead of cloning
everything.

## Package layout

```
agentcache/           Python package (the server-side tool)
  __init__.py         GENERATOR_VERSION constant — bump on schema changes
  config.py           AgentCacheConfig frozen dataclass, all AGENTCACHE_* env vars
  manifest.py         build_manifest()  — flat path→{oid,size,mode} via Index.read_tree
  symbols.py          build_symbol_index() — universal-ctags JSON; degrades w/o ctags
  cache_writer.py     write_cache() / read_artifact() — orphan commit storage
  bundle.py           create_blobless_bundle() / verify_bundle()
  hook.py             post-receive orchestration; main() is fail-open (never blocks push)
  service.py          Flask app: /healthz /caches /cache/<c>/manifest /cache/<c>/symbol/<n>
                       /cache/<c>/resolve (POST)

hooks/post-receive    Shell shim: exec python3 -m agentcache.hook

tests/
  conftest.py         repo + cfg fixtures; FILES dict (TokenRefresher, make_refresher, str_len)
  test_manifest.py    completeness, sort order, OID/size accuracy
  test_symbols.py     ctags degradation + symbol indexing (skipped if ctags absent)
  test_cache_writer.py orphan commit, side ref, readback, idempotency
  test_service.py     Flask endpoints, resolve returns correct fetch_oids
  test_bundle_and_coldstart.py  end-to-end: blobless clone → verify blobs absent → targeted fetch

benchmark/            Stand-alone benchmarking scripts (not part of the installed package)
  approaches/         naive.py / blobless.py / agentcache.py
  run.py              --smoke mode requires no setup; full mode requires a local repo
  setup_repo.sh       Mirror a local repo into benchmark/repos/, install hook, generate cache

testharness/          Local web-based test harness (FastAPI + Alpine.js + Chart.js)
  start.sh            One-command startup: git daemon + proxy + agentcache svc + web UI
  app.py              FastAPI routes, lifespan manages all subprocesses
  proxy.py            Transparent TCP byte-counting proxy (port 9419 → 9418)
  runner.py           Orchestrates the three approaches; emits SSE events
```

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest -q          # 20 passed, 1 skipped (ctags absent — expected)
```

The single skipped test (`test_indexes_known_symbols`) only runs when
universal-ctags is installed.  All other tests are self-contained and
create ephemeral bare repos in `tmp_path`.

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

Requires repos in `benchmark/repos/`.  See `benchmark/README.md`.

## Key design invariants — do not break these

| Invariant | Why |
|-----------|-----|
| `hook.main()` is **fail-open** — exceptions log but never exit non-zero | A cache failure must never block a push |
| Cache commits have **no parents** (`parents=[]`) | Orphan commits never appear in normal history walks or `git log` |
| Artifact names must be **flat** (no `/`) | `write_cache()` enforces this; tree depth is always 1 |
| `build_manifest()` uses `Index.read_tree()` | Do not replace with hand-rolled recursion — Index handles submodule entries correctly |
| `GENERATOR_VERSION` is embedded in every artifact | Bump it if the artifact schema changes so stale caches can be detected |

## Coding conventions

- All source files use `from __future__ import annotations`.
- Public functions have docstrings.
- Tests use `tmp_path`; never write to the working tree.
- The Flask service (`service.py`) must remain importable without a
  running repo — `create_app(cfg)` is the only entry point.
- Do not add runtime dependencies beyond `requirements.txt` without
  discussion.  The install footprint is intentionally tiny.
