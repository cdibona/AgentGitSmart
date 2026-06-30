#!/usr/bin/env python3
"""Thin shim — delegates to agentcache.generate.main.

The real implementation lives in the installable package so adopters can invoke
it as the ``agentcache-generate`` console script without the AgentCache source
tree on sys.path.  This script exists for backward compat and for running
directly from a checkout.

Output contract (see agentcache/generate.py for details):
  source_commit : <sha>
  cache_ref     : refs/agent-cache/<sha>
  cache_commit  : <orphan-sha>
  manifest      : N entries
  symbols       : N (ctags=yes|no)
  [bundle        : <path>]
  ::AGENTCACHE_REF::refs/agent-cache/<sha>
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agentcache.generate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
