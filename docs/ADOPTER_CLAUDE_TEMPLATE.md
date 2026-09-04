# CLAUDE.md — `<REPO_NAME>`

> **This repo has agentgitsmart installed. Do not clone all of it.**
>
> A full or shallow `git clone` downloads every blob at HEAD and defeats the
> entire point. Use the cold-start protocol in `AGENTS.md` instead: a blobless
> clone, then fetch only the blobs you are actually going to read.

Agent instructions for this repository live in **`AGENTS.md`**, the tool-neutral
convention that Codex CLI, opencode, Cursor and others also read. Keeping them
in one file means Claude Code and every other agent get the same instructions,
and there is only one file to keep correct.

@AGENTS.md

If that import did not resolve for any reason, open `AGENTS.md` directly and
follow the cold-start protocol there before touching the repo. The short version:

```bash
# blobless clone: full history + trees, ZERO file content
git clone --filter=blob:none --no-checkout <REPO_URL> repo && cd repo
C=$(git rev-parse HEAD)

# read the pre-built manifest / symbol index for this exact commit
git fetch origin "refs/agent-git-smart/$C:refs/agent-git-smart/$C"
git cat-file -p "refs/agent-git-smart/$C:manifest.json"   # .entries[] -> {path,oid,size,mode}
git cat-file -p "refs/agent-git-smart/$C:symbols.json"    # .symbols -> {name: [{path,line,kind}]}

# fetch ONLY the blobs you need, in ONE batched fetch, then read them by OID
git fetch origin <oid> <oid> ...
git cat-file blob <oid>
```

`.agentgitsmart` in the repo root carries the machine-readable version of the
same discovery information.

---

*Add this repository's own project instructions — build commands, conventions,
test invocations — below this line.*
