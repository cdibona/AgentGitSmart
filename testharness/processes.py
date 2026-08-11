"""Manage git daemon and agentgitsmart service subprocesses.

Both processes are started during the FastAPI lifespan and terminated
on shutdown.  The agentgitsmart service is per-repo, so it can be
restarted with switch_repo() when the user selects a different repo.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
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


async def _wait_http_ready(host: str, port: int, timeout: float = 8.0) -> bool:
    """Poll until the HTTP server actually answers a request (not just bound).

    A bound TCP port does NOT mean Flask is ready to serve — the very first
    request can race the worker coming up and fail with a connection reset,
    which previously made the first agentgitsmart pass spuriously look 'cold'.
    Any HTTP status (even 404) proves the app is serving.
    """
    deadline = time.monotonic() + timeout
    path = f"http://{host}:{port}/healthz"
    while time.monotonic() < deadline:
        try:
            def _probe() -> int:
                import urllib.error
                import urllib.request
                try:
                    return urllib.request.urlopen(path, timeout=2).status  # noqa: S310
                except urllib.error.HTTPError as e:   # 404 etc. == serving
                    return e.code

            code = await asyncio.get_event_loop().run_in_executor(None, _probe)
            if code:
                return True
        except Exception:
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


class AgentGitSmartService:
    """Wrap agentgitsmart Flask service as an asyncio subprocess.

    The service is bound to a single repo directory.  Call
    switch_repo() to restart it against a different repo.
    """

    def __init__(self, port: int = 8765) -> None:
        self.port = port
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._current_repo: Optional[str] = None
        self._log_fh = None

    async def start(self, repo_dir: str) -> bool:
        if self._proc and self._proc.returncode is None:
            if self._current_repo == repo_dir:
                return True  # already running for this repo
            await self.stop()

        env = dict(os.environ)
        env["AGENTGITSMART_REPO_DIR"] = repo_dir
        env["AGENTGITSMART_SERVICE_PORT"] = str(self.port)
        env["AGENTGITSMART_SERVICE_HOST"] = "127.0.0.1"

        # Capture the service's stderr to a logfile so build/serve failures are
        # diagnosable (was DEVNULL, which silently hid 500s e.g. for repos with
        # submodules).  Appended so a switch_repo restart keeps prior history.
        log_path = os.environ.get(
            "AGENTGITSMART_SERVICE_LOG",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "testharness", "data", "agentgitsmart-service.log"),
        )
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            self._log_fh = open(log_path, "a")  # noqa: SIM115
        except OSError:
            self._log_fh = None

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "agentgitsmart.service",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=(self._log_fh if self._log_fh else asyncio.subprocess.DEVNULL),
        )
        ok = await _wait_port("127.0.0.1", self.port, timeout=6.0)
        if ok:
            # Port bound != Flask ready; wait until it actually answers so the
            # first agentgitsmart pass doesn't spuriously look 'cold'.
            await _wait_http_ready("127.0.0.1", self.port, timeout=8.0)
            self._current_repo = repo_dir
            log.info("agentgitsmart service: port %d (repo: %s)", self.port, repo_dir)
        else:
            log.warning("agentgitsmart service: did not bind within timeout")
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
        log.info("agentgitsmart service: stopped")

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
