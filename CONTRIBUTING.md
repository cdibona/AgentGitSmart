# Contributing to AgentGitSmart

You can contribute with issues and pull requests. Filing a good issue — especially
one with measurements attached — is a real contribution on its own.

Please read the [proof-of-concept caveat](README.md) first. This is an
experimental prototype for exploring and measuring server-side per-commit agent
caching, not production-hardened software. Contributions that sharpen the
measurements are as welcome as contributions that add features.

## Reporting issues

New issues go in [the issue list](https://github.com/cdibona/AgentGitSmart/issues).
Search it first — if your problem is already there, add your own data to the
discussion rather than opening a duplicate.

There are three templates. Pick the one that fits:

- **Bug report** — something behaves differently than documented.
- **Measurement report** — you ran the trial on a repo and want to share what
  it said. These are genuinely useful: the project's central claim is that
  agentgitsmart only pays off on *some* repos, and every honest measurement —
  especially a `blobless is enough` one — helps map where the line is.
- **Feature request** — an idea or a use case that isn't covered.

Security vulnerabilities do **not** go in the issue tracker. See
[SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.10+, `git`, and `universal-ctags`. Without ctags the symbol
index is empty and about a dozen tests skip, so install it:

```bash
sudo apt-get install -y universal-ctags      # Debian/Ubuntu
brew install universal-ctags                 # macOS
```

Then:

```bash
git clone https://github.com/cdibona/AgentGitSmart
cd AgentGitSmart
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the tests

```bash
pytest -q                       # 275 tests
pytest testharness/tests -q     # 34 tests (3 skip without psutil)
python -m benchmark.run --smoke # end-to-end smoke, no setup required
```

All three should pass before you open a PR. `git status` must still be clean
afterward — see the invariants below.

## Things this project cares about

These are the conventions most likely to trip you up, and the ones a reviewer
will check first.

**Measure before you claim.** This is a measurement project, so its own claims
have to hold up. Any number in the docs must be recomputed from the committed
JSON under `experiments/results/` — never carried over from an older run,
estimated, or rounded in a flattering direction. If you change a number, say
which file you recomputed it from.

**Tests never write to the working tree.** Use `tmp_path`. If `git status` is
dirty after a test run, that's a bug in the test.

**The hook is fail-open.** `hook.main()` logs errors and still exits 0. A cache
failure must never block someone's push. Don't add a code path that can exit
non-zero from the hook.

**Cache commits are orphans** (`parents=[]`) with flat artifact names, so they
never appear in `git log` and never link into project history.

**Delta output must match a full rebuild.** The `symbols` payload from a delta
build has to be byte-identical to a full one, in identical key order.
`canonicalize_symbols()` is the single chokepoint — route every symbol merge
through it.

**Bump the versions on schema changes.** `GENERATOR_VERSION` in
`agentgitsmart/__init__.py` and `SYMBOLS_SCHEMA` in `agentgitsmart/symbols.py`
are how stale caches get detected.

**Keep the install footprint small.** Don't add runtime dependencies beyond
`requirements.txt` without discussing it in an issue first.

More detail on all of this is in [AGENTS.md](AGENTS.md) and
[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Pull requests

- Branch off `main` and keep the change focused on one thing.
- Update the docs in the same PR as the code. Stale docs are the failure mode
  this project is most prone to, because so much of it is documentation of an
  on-disk format.
- Say what you actually ran, and paste the output. "Tests pass" is less useful
  than the test summary line.
- If the change affects measured behavior, re-run the relevant study and commit
  the regenerated JSON alongside it.

## Contributor License Agreement

This project welcomes contributions and suggestions. Most contributions require
you to agree to a Contributor License Agreement (CLA) declaring that you have
the right to, and actually do, grant us the rights to use your contribution. For
details, visit <https://cla.opensource.microsoft.com>.

On repositories in the Microsoft organization a CLA bot decorates each pull
request automatically. This repository does not have that bot installed yet, so
if a CLA becomes necessary a maintainer will tell you in the PR. You only need
to sign once across all repos using the Microsoft CLA.

## Code of Conduct

This project has adopted the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the
[Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any
additional questions or comments.
