# CLAUDE.md — AgentGitSmart

> **Do not clone all of this repo.** It publishes its own agent-knowledge cache
> on every push to `main` as a git side ref, so you can read the manifest and
> symbol index for the exact commit you're on and then fetch only the blobs you
> actually need. A full or shallow `git clone` defeats the entire point of the
> project you are working on.

The instructions for working on this codebase live in **`AGENTS.md`** — the
tool-neutral convention that Codex CLI, opencode, Cursor and others read too.
Keeping one file means every agent gets the same instructions and there is only
one document to keep correct.

@AGENTS.md

If that import did not resolve, read `AGENTS.md` directly. The cold start, in
short:

```bash
# blobless clone: full history + trees, ZERO file content
git clone --filter=blob:none --no-checkout https://github.com/cdibona/AgentGitSmart repo
cd repo && C=$(git rev-parse HEAD)

# this repo is service-less — read the cache straight from the side ref
git fetch origin "refs/agent-git-smart/$C:refs/agent-git-smart/$C"
git cat-file -p "refs/agent-git-smart/$C:manifest.json"   # .entries[] -> {path,oid,size,mode}
git cat-file -p "refs/agent-git-smart/$C:symbols.json"    # .symbols -> {name: [{path,line,kind}]}

# fetch ONLY the blobs you'll read, in ONE batched fetch, then read by OID
git fetch origin <oid> <oid> ...
git cat-file blob <oid>
```

## Before you claim anything works

This project measures things, so its own claims have to hold up:

- Run `pytest -q` (275 tests) and `pytest testharness/tests -q` before saying a
  change works. Install `universal-ctags` first or ~a dozen symbol tests skip.
- Tests must never write to the working tree. If `git status` is dirty after a
  test run, that is a bug in the test, not a normal outcome.
- Numbers in the docs must be recomputed from the committed JSON under
  `experiments/results/` — never carried over from an older run or estimated.
