"""Configuration, loaded from the environment / a .env file.

Every knob has an ``AGENTCACHE_`` prefix so it composes cleanly with a git
hook environment. Nothing here is secret; auth tokens for the promisor and the
index service are handled by the surrounding infra, not this process.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


def _bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentCacheConfig:
    # The repository we generate caches for. On a server this is the bare repo;
    # a post-receive hook gets it from $GIT_DIR.
    repo_dir: str
    # Side-ref namespace. Kept out of refs/heads so it never shows up as a
    # branch and never drags into normal history walks.
    ref_prefix: str = "refs/agent-cache"
    # Symbol indexer. universal-ctags recommended; absence degrades gracefully.
    ctags_bin: str = "ctags"
    # Where bootstrap bundles are written (then synced to object storage / CDN).
    # None disables bundle generation.
    bundle_dir: Optional[str] = None
    bundle_filter: str = "blob:none"
    # Identity stamped on the orphan cache commits.
    bot_name: str = "AgentCache Bot"
    bot_email: str = "agentcache@localhost"
    # Query service bind.
    service_host: str = "127.0.0.1"
    service_port: int = 8765

    @classmethod
    def from_env(cls, env_path: Optional[str] = None, *, repo_dir: Optional[str] = None) -> "AgentCacheConfig":
        # load_dotenv is a no-op if the file is absent, so this is safe in prod
        # where config arrives as real environment variables.
        load_dotenv(env_path, override=False)
        repo = repo_dir or os.environ.get("AGENTCACHE_REPO_DIR") or os.environ.get("GIT_DIR")
        if not repo:
            raise ValueError(
                "repo_dir not set: pass repo_dir=, or set AGENTCACHE_REPO_DIR / GIT_DIR"
            )
        bundle_dir = os.environ.get("AGENTCACHE_BUNDLE_DIR") or None
        return cls(
            repo_dir=repo,
            ref_prefix=os.environ.get("AGENTCACHE_REF_PREFIX", "refs/agent-cache"),
            ctags_bin=os.environ.get("AGENTCACHE_CTAGS_BIN", "ctags"),
            bundle_dir=bundle_dir,
            bundle_filter=os.environ.get("AGENTCACHE_BUNDLE_FILTER", "blob:none"),
            bot_name=os.environ.get("AGENTCACHE_BOT_NAME", "AgentCache Bot"),
            bot_email=os.environ.get("AGENTCACHE_BOT_EMAIL", "agentcache@localhost"),
            service_host=os.environ.get("AGENTCACHE_SERVICE_HOST", "127.0.0.1"),
            service_port=int(os.environ.get("AGENTCACHE_SERVICE_PORT", "8765")),
        )
