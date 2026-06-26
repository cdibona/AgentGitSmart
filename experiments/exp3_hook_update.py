"""Experiment 3 — A human (non-agentcache-aware) push updates the cache.

The agentcache cache is keyed by commit OID, so when a human pushes a NEW
commit the cache for the new HEAD must be (re)built.  The mechanism for that
is the server-side ``post-receive`` hook: it fires on every push, sees the new
branch tip, and writes ``refs/agent-cache/<new-oid>`` automatically — no agent
involvement, no agentcache-awareness on the human's part.

This experiment proves the loop end to end on a throwaway bare repo:

  1. Create a fresh bare repo, install the agentcache post-receive hook.
  2. Human clones, edits a file, commits, pushes  → hook fires.
  3. Verify a cache ref for the NEW commit now exists, and its manifest
     reflects the human's change (the edited/added path is present).
  4. Push a SECOND commit → verify a second cache ref appears.

Deterministic and fast (synthetic repo); no network, no Docker.

Output: experiments/results/exp3_hook_update.json + printed verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pygit2

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agentcache import cache_writer  # noqa: E402
from agentcache import uninstall as uninstall_mod  # noqa: E402
from agentcache import symbols as symbols_mod  # noqa: E402
from experiments import harness as harness_mod  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
REF_PREFIX = "refs/agent-cache"
HOOK_TEMPLATE = """#!/bin/sh
# agentcache post-receive shim (installed by exp3)
export PYTHONPATH="{root}:${{PYTHONPATH:-}}"
export AGENTCACHE_CTAGS_BIN="{ctags}"
exec "{python}" -m agentcache.hook
"""


def _git(*args, cwd=None, env=None, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check
    )


def _install_hook(bare_repo: str) -> str:
    hook_path = os.path.join(bare_repo, "hooks", "post-receive")
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    # ctags is optional — pass through whatever's on PATH (symbols degrade gracefully).
    ctags = (
        subprocess.run(
            ["which", "ctags"], capture_output=True, text=True
        ).stdout.strip()
        or "ctags"
    )
    with open(hook_path, "w") as fh:
        fh.write(
            HOOK_TEMPLATE.format(root=str(_ROOT), python=sys.executable, ctags=ctags)
        )
    os.chmod(hook_path, 0o755)
    return hook_path


def _human_push(
    bare_repo: str, work: str, branch: str, fname: str, content: str, msg: str
) -> dict:
    """Simulate a human: clone (if needed), write a file, commit, push. Returns push stderr."""
    clone = os.path.join(work, "clone")
    if not os.path.exists(clone):
        _git("clone", bare_repo, clone)
        _git("config", "user.email", "human@example.com", cwd=clone)
        _git("config", "user.name", "A Human", cwd=clone)
    with open(os.path.join(clone, fname), "w") as fh:
        fh.write(content)
    _git("add", fname, cwd=clone)
    _git("commit", "-m", msg, cwd=clone)
    new_oid = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    push = _git("push", "origin", f"HEAD:{branch}", cwd=clone)
    return {"new_oid": new_oid, "hook_stderr": push.stderr.strip()}


def run(branch: str = "main") -> dict:
    work = tempfile.mkdtemp(prefix="exp3-")
    bare = os.path.join(work, "repo.git")
    steps = []
    try:
        # 1. fresh bare repo with the hook installed
        _git("init", "--bare", f"--initial-branch={branch}", bare)
        # allow the synthetic repo to accept pushes to the checked-out branch
        _git("config", "receive.denyCurrentBranch", "ignore", cwd=bare)
        hook_path = _install_hook(bare)

        repo = pygit2.Repository(bare)
        assert uninstall_mod.find_cache_refs(repo, REF_PREFIX) == [], (
            "should start empty"
        )

        # 2. + 3. first human push → hook should build a cache for the new commit
        p1 = _human_push(
            bare,
            work,
            branch,
            "hello.py",
            "# hello\nclass Greeter:\n    pass\n",
            "add Greeter",
        )
        repo = pygit2.Repository(bare)
        ref1 = f"{REF_PREFIX}/{p1['new_oid']}"
        built1 = ref1 in repo.references
        # the manifest should reflect the human's new file
        path_in_manifest1 = False
        if built1:
            raw = cache_writer.read_artifact(
                repo, p1["new_oid"], "manifest.json", ref_prefix=REF_PREFIX
            )
            paths = {e["path"] for e in json.loads(raw)["entries"]}
            path_in_manifest1 = "hello.py" in paths
        # Load the generation block for the first push
        load1: dict = {}
        if built1:
            try:
                gen1 = harness_mod.load_report_for(
                    repo, p1["new_oid"], ref_prefix=REF_PREFIX
                )
                load1 = {
                    "mode": gen1.get("mode"),
                    "parent": gen1.get("parent"),
                    "files_reindexed": gen1.get("files_reindexed"),
                    "files_carried_forward": gen1.get("files_carried_forward"),
                    "content_bytes_materialized": gen1.get(
                        "content_bytes_materialized"
                    ),
                }
            except Exception:
                pass
        steps.append(
            {
                "step": "first_push",
                "new_oid": p1["new_oid"],
                "cache_built": built1,
                "edited_path_in_manifest": path_in_manifest1,
                "hook_stderr": p1["hook_stderr"],
                "load": load1,
            }
        )
        print(
            f"[1] human push {p1['new_oid'][:12]} -> cache_built={built1} "
            f"path_in_manifest={path_in_manifest1} mode={load1.get('mode')}"
        )
        print(f"    hook said: {p1['hook_stderr']}")

        # 4. second human push → a second, distinct cache ref appears
        p2 = _human_push(
            bare,
            work,
            branch,
            "world.py",
            "# world\ndef compute():\n    return 42\n",
            "add compute",
        )
        repo = pygit2.Repository(bare)
        ref2 = f"{REF_PREFIX}/{p2['new_oid']}"
        built2 = ref2 in repo.references
        total_refs = len(uninstall_mod.find_cache_refs(repo, REF_PREFIX))
        # Load the generation block for the second push
        load2: dict = {}
        if built2:
            try:
                gen2 = harness_mod.load_report_for(
                    repo, p2["new_oid"], ref_prefix=REF_PREFIX
                )
                load2 = {
                    "mode": gen2.get("mode"),
                    "parent": gen2.get("parent"),
                    "files_reindexed": gen2.get("files_reindexed"),
                    "files_carried_forward": gen2.get("files_carried_forward"),
                    "content_bytes_materialized": gen2.get(
                        "content_bytes_materialized"
                    ),
                }
            except Exception:
                pass
        steps.append(
            {
                "step": "second_push",
                "new_oid": p2["new_oid"],
                "cache_built": built2,
                "total_cache_refs": total_refs,
                "hook_stderr": p2["hook_stderr"],
                "load": load2,
            }
        )
        print(
            f"[2] human push {p2['new_oid'][:12]} -> cache_built={built2} "
            f"total_cache_refs={total_refs} mode={load2.get('mode')} "
            f"files_reindexed={load2.get('files_reindexed')}"
        )
        print(f"    hook said: {p2['hook_stderr']}")

        # Verdict: base checks + load mode assertions (gated on ctags availability).
        _ctags_ok = symbols_mod.ctags_available()
        first_load_ok = load1.get("mode") == "full"
        if _ctags_ok:
            second_load_ok = (
                load2.get("mode") == "delta" and load2.get("files_reindexed") == 1
            )
        else:
            second_load_ok = load2.get("mode") == "full"

        verdict = (
            "PASS"
            if (
                built1
                and path_in_manifest1
                and built2
                and total_refs == 2
                and first_load_ok
                and second_load_ok
            )
            else "FAIL"
        )
        print(f"\n==> Hook-driven cache update: {verdict}")

        report = {
            "experiment": "hook_update",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "branch": branch,
            "hook_path": hook_path,
            "verdict": verdict,
            "steps": steps,
        }
    finally:
        subprocess.run(["rm", "-rf", work], check=False)

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "exp3_hook_update.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"Wrote {out}")
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--branch", default="main")
    args = p.parse_args(argv)
    report = run(args.branch)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
