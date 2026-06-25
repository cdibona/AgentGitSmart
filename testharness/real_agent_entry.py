"""In-container driver for the real agent task.

Runs inside the Docker container. Calls real_agent.run_real_agent() and
emits sentinel-wrapped JSON to stdout so docker_runner can extract the result.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

PACK_ROOT = "/pack"
if PACK_ROOT not in sys.path:
    sys.path.insert(0, PACK_ROOT)

from testharness.real_agent import run_real_agent  # noqa: E402

_START = "__PACK_RESULT__"
_END = "__PACK_END__"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--approach", required=True)
    p.add_argument("--repo-url", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--service-url", default="http://127.0.0.1:8765")
    p.add_argument("--pct", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    try:
        metrics = run_real_agent(
            approach=args.approach,
            repo_url=args.repo_url,
            commit=args.commit,
            branch=args.branch,
            service_url=args.service_url,
            pct=args.pct,
            seed=args.seed,
        )
        payload = {"result": metrics, "used_docker": True}
        sys.stdout.write(f"\n{_START}{json.dumps(payload)}{_END}\n")
        sys.stdout.flush()
        return 0 if not metrics.get("error") else 1

    except Exception as exc:
        err = {"error": str(exc), "traceback": traceback.format_exc()}
        sys.stdout.write(f"\n{_START}{json.dumps(err)}{_END}\n")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
