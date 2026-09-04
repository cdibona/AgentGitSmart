<!--
Thanks for the PR. Keep it focused on one thing, and delete any section below
that doesn't apply. See CONTRIBUTING.md for the full guidance.
-->

## What this changes

<!-- What does it do, and why? Link the issue if there is one (e.g. Fixes #12). -->

## What you ran

<!--
Paste the actual output, not a summary. "Tests pass" is less useful than the
summary line. Delete the rows you didn't run, and say why.
-->

| Command | Result |
|---|---|
| `pytest -q` | <!-- e.g. 275 passed in 8.4s --> |
| `pytest testharness/tests -q` | <!-- e.g. 34 passed, 3 skipped --> |
| `python -m benchmark.run --smoke` | <!-- e.g. ran, 3 arms compared --> |

- [ ] `git status` is clean after running the tests
      <!-- Tests must never write to the working tree. If a committed file under
           experiments/results/ changed, that's a bug in the test. -->

## Checklist

- [ ] Docs updated in this same PR
      <!-- Stale docs are this project's most common failure: much of it is
           documentation of an on-disk format. If you changed the artifact
           shape, an endpoint, an env var, or a default, find every place that
           documents it. -->
- [ ] Any number I changed was recomputed from the committed JSON under
      `experiments/results/` — say which file below, and don't estimate
- [ ] If measured behavior changed, I re-ran the relevant study and committed
      the regenerated JSON

## Invariants

Tick only what your change touches — these are what a reviewer checks first.

- [ ] The hook is still **fail-open**: no new path can make `hook.main()` exit
      non-zero. A cache failure must never block someone's push.
- [ ] Cache commits are still **orphans** (`parents=[]`) with flat artifact names.
- [ ] Delta symbol output is still **byte-identical** to a full rebuild, in
      identical key order — every merge routed through `canonicalize_symbols()`.
      (Only the envelope's wall-clock `generated_at` may differ.)
- [ ] `GENERATOR_VERSION` and/or `SYMBOLS_SCHEMA` bumped, if the artifact
      schema changed.
- [ ] No new runtime dependency beyond `requirements.txt` — or explained below
      why one is necessary.

## Anything else

<!--
Trade-offs, things you weren't sure about, or things you deliberately left out.
Saying "I didn't do X because Y" is genuinely helpful.
-->
