"""Manage git daemon and agentcache service subprocesses.

Both processes are started during the FastAPI lifespan and terminated
on shutdown.  The agentcache service is per-repo, so it can be
restarted with switch_repo() when the user selects a different repo.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


async def _wait_port(host: str, port: int, timeout: float = 8.0) -> bool:
    """Poll until a TCP port accepts connections or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, w = await asyncio.open_connection(host, port)
            w.close()
            return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.15)
    return False


class GitDaemon:
    """Wrap git daemon as an asyncio subprocess."""

    def __init__(self, repos_dir: str, port: int = 9418) -> None:
        self.repos_dir = os.path.abspath(repos_dir)
        self.port = port
        self._proc: Optional[asyncio.subprocess.Process] = None

    async def start(self) -> bool:
        if self._proc and self._proc.returncode is None:
            return True  # already running
        os.makedirs(self.repos_dir, exist_ok=True)
        cmd = [
            "git", "daemon",
            f"--port={self.port}",
            "--reuseaddr",
            "--export-all",
            "--verbose",
            f"--base-path={self.repos_dir}",
            self.repos_dir,
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ok = await _wait_port("127.0.0.1", self.port, timeout=6.0)
        if ok:
            log.info("git daemon: listening on port %d (repos: %s)", self.port, self.repos_dir)
        else:
            log.warning("git daemon: did not bind within timeout")
        return ok

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                self._proc.kill()
        log.info("git daemon: stopped")

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None


class AgentCacheService:
    """Wrap agentcache Flask service as an asyncio subprocess.

    The service is bound to a single repo directory.  Call
    switch_repo() to restart it against a different repo.
    """

    def __init__(self, port: int = 8765) -> None:
        self.port = port
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._current_repo: Optional[str] = None

    async def start(self, repo_dir: str) -> bool:
        if self._proc and self._proc.returncode is None:
            if self._current_repo == repo_dir:
                return True  # already running for this repo
            await self.stop()

        env = dict(os.environ)
        env["AGENTCACHE_REPO_DIR"] = repo_dir
        env["AGENTCACHE_SERVICE_PORT"] = str(self.port)
        env["AGENTCACHE_SERVICE_HOST"] = "127.0.0.1"

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "agentcache.service",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ok = await _wait_port("127.0.0.1", self.port, timeout=6.0)
        if ok:
            self._current_repo = repo_dir
            log.info("agentcache service: port %d (repo: %s)", self.port, repo_dir)
        else:
            log.warning("agentcache service: did not bind within timeout")
        return ok

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                self._proc.kill()
        self._proc = None
        self._current_repo = None
        log.info("agentcache service: stopped")

    async def switch_repo(self, repo_dir: str) -> bool:
        """(Re)start the service for repo_dir if different from current."""
        if self._current_repo == repo_dir and self.is_running:
            return True
        return await self.start(repo_dir)

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def current_repo(self) -> Optional[str]:
        return self._current_repo
