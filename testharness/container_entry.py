"""In-container driver: runs one benchmark approach then the agent task, emits JSON."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback

PACK_ROOT = "/pack"
if PACK_ROOT not in sys.path:
    sys.path.insert(0, PACK_ROOT)

# These imports work because PYTHONPATH=/pack and the modules are stdlib-only
from benchmark.approaches import naive, blobless  # noqa: E402
from benchmark.approaches import agentcache as ac_approach  # noqa: E402
from testharness.agent_task import run_agent_task  # noqa: E402

_SENTINEL_START = "__PACK_RESULT__"
_SENTINEL_END = "__PACK_END__"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--approach", required=True)
    p.add_argument("--repo-url", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--service-url", default="http://127.0.0.1:8765")
    p.add_argument("--symbol", default="ClassDef")
    p.add_argument("--target", action="append", dest="targets", default=[])
    args = p.parse_args()

    work_dir = tempfile.mkdtemp(prefix="pack-", dir="/tmp")
    try:
        t0 = time.monotonic()
        approach = args.approach
        targets = args.targets or ["Lib/ast.py"]

        if approach == "naive":
            clone = naive.run(args.repo_url, args.branch, targets, work_dir)
        elif approach == "blobless":
            clone = blobless.run(args.repo_url, args.commit, args.branch, targets, work_dir)
        elif approach == "agentcache":
            clone = ac_approach.run(args.repo_url, args.commit, args.branch,
                                     args.service_url, targets, work_dir)
        else:
            raise ValueError(f"Unknown approach: {approach!r}")

        clone_ms = round((time.monotonic() - t0) * 1000.0, 1)
        workspace = os.path.join(work_dir, "workspace")

        agent = run_agent_task(
            approach, workspace, args.commit, targets, args.service_url, args.symbol
        )

        out = {"clone": clone, "clone_ms": clone_ms, "agent_task": agent, "used_docker": True}
        sys.stdout.write(f"\n{_SENTINEL_START}{json.dumps(out)}{_SENTINEL_END}\n")
        sys.stdout.flush()
        return 0

    except Exception as exc:
        err = {"error": str(exc), "traceback": traceback.format_exc()}
        sys.stdout.write(f"\n{_SENTINEL_START}{json.dumps(err)}{_SENTINEL_END}\n")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
