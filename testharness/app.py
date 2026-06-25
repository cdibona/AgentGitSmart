"""FastAPI test harness — entry point.

Lifecycle
---------
  startup  → start git daemon → start counting proxy → (agentcache
             service starts on first test run, per repo)
  shutdown → stop proxy → stop git daemon → stop agentcache service

Ports (all localhost)
---------------------
  9418  git daemon  (git:// protocol)
  9419  counting proxy  (git:// forwarded to 9418, bytes counted)
  8765  agentcache query service
  8080  this web app
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import docker_runner
from .models import RunConfig, SystemStatus
from .processes import AgentCacheService, GitDaemon
from .proxy import ByteCountingProxy
from .runner import TestRunner
from .storage import ResultStorage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_REPOS_DIR = str(_REPO_ROOT / "benchmark" / "repos")
_DB_PATH = str(_HERE / "data" / "runs.db")

GIT_PORT = int(os.environ.get("AGENTCACHE_GIT_PORT", "9418"))
PROXY_PORT = int(os.environ.get("AGENTCACHE_PROXY_PORT", "9419"))
SVC_PORT = int(os.environ.get("AGENTCACHE_SVC_PORT", "8765"))
WEB_PORT = int(os.environ.get("AGENTCACHE_WEB_PORT", "8080"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state (populated during lifespan)
# ---------------------------------------------------------------------------

_git_daemon: Optional[GitDaemon] = None
_proxy: Optional[ByteCountingProxy] = None
_agentcache_svc: Optional[AgentCacheService] = None
_runner: Optional[TestRunner] = None
_storage: Optional[ResultStorage] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kill_stale_port_holder(port: int) -> None:
    """Terminate any process already bound to *port* (prevents EADDRINUSE)."""
    try:
        r = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            my_pid = os.getpid()
            for pid_s in r.stdout.strip().split():
                pid = int(pid_s)
                if pid != my_pid:
                    os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
    except Exception:
        pass


def _list_repos() -> list[str]:
    d = Path(_REPOS_DIR)
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "HEAD").exists())


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _git_daemon, _proxy, _agentcache_svc, _runner, _storage

    _storage = ResultStorage(_DB_PATH)

    _kill_stale_port_holder(PROXY_PORT)
    _kill_stale_port_holder(WEB_PORT)

    _git_daemon = GitDaemon(_REPOS_DIR, port=GIT_PORT)
    _proxy = ByteCountingProxy("127.0.0.1", PROXY_PORT, "127.0.0.1", GIT_PORT)
    _agentcache_svc = AgentCacheService(port=SVC_PORT)

    git_ok = await _git_daemon.start()
    if not git_ok:
        log.warning(
            "git daemon did not start cleanly — tests that clone via git:// will fail"
        )

    await _proxy.start()

    _runner = TestRunner(
        proxy=_proxy,
        repos_dir=_REPOS_DIR,
        git_proxy_port=PROXY_PORT,
        agentcache_port=SVC_PORT,
    )

    log.info("Test harness ready on http://127.0.0.1:%d", WEB_PORT)
    yield

    await _proxy.stop()
    await _git_daemon.stop()
    await _agentcache_svc.stop()
    log.info("Test harness shut down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="PackCache Test Harness", lifespan=lifespan)

# Mount static files
_STATIC_DIR = _HERE / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def status() -> SystemStatus:
    return SystemStatus(
        git_daemon=_git_daemon.is_running if _git_daemon else False,
        git_daemon_port=GIT_PORT,
        proxy=_proxy.is_running if _proxy else False,
        proxy_port=PROXY_PORT,
        agentcache_service=_agentcache_svc.is_running if _agentcache_svc else False,
        agentcache_port=SVC_PORT,
        repos=_list_repos(),
        docker_available=docker_runner.is_docker_available(),
    )


@app.get("/api/repos")
async def list_repos() -> dict:
    return {"repos": _list_repos()}


@app.get("/api/runs")
async def list_runs() -> dict:
    assert _storage
    return {"runs": [r.model_dump() for r in _storage.list_runs()]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    assert _storage
    run = _storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.model_dump()


@app.post("/api/runs")
async def start_run(config: RunConfig) -> dict:
    assert _runner and _storage and _agentcache_svc

    if not _list_repos():
        raise HTTPException(
            status_code=400,
            detail=(
                "No repos found in benchmark/repos/. "
                "Run: bash benchmark/setup_repo.sh --source /your/local/repo benchmark/repos/myrepo.git"
            ),
        )

    run_id = uuid.uuid4().hex[:8]
    created_at = datetime.now(timezone.utc).isoformat()

    _storage.create_run(run_id, created_at, config)
    _runner.register_queue(run_id)

    # Start the agentcache service for this repo (restart if needed).
    repo_path = str(Path(_REPOS_DIR) / config.repo_name)
    asyncio.create_task(_start_and_run(run_id, config, repo_path))

    return {"run_id": run_id, "status": "running"}


async def _start_and_run(run_id: str, config: RunConfig, repo_path: str) -> None:
    assert _runner and _storage and _agentcache_svc
    try:
        svc_ok = await _agentcache_svc.switch_repo(repo_path)
        if not svc_ok and "agentcache" in config.approaches:
            _runner._log(
                run_id,
                "WARNING: agentcache service did not start; agentcache approach may fail",
            )

        results = await _runner.execute(
            run_id=run_id,
            repo_name=config.repo_name,
            branch=config.branch,
            target_paths=config.target_paths,
            approaches=config.approaches,
            num_runs=config.num_runs,
            use_docker=config.use_docker,
            latency_ms=config.latency_ms,
        )
        _storage.finish_run(run_id, "complete", results)
    except Exception as exc:
        log.exception("run %s failed", run_id)
        _runner._emit(run_id, "error", msg=str(exc))
        _storage.finish_run(run_id, "error")


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Server-Sent Events stream for a running test."""
    assert _runner and _storage

    run = _storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # If the run is already complete, stream the results immediately.
    if run.status in ("complete", "error"):

        async def _replay() -> AsyncIterator[str]:
            yield f"data: {json.dumps({'type': 'run_complete', 'results': [r.model_dump() for r in run.results]})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

        return StreamingResponse(_replay(), media_type="text/event-stream")

    # Live stream from the runner's queue.
    queue = _runner._queues.get(run_id)
    if queue is None:
        # Race: run finished between the status check and here.
        run2 = _storage.get_run(run_id)
        if run2 and run2.status in ("complete", "error"):

            async def _replay2() -> AsyncIterator[str]:
                yield f"data: {json.dumps({'type': 'run_complete', 'results': [r.model_dump() for r in run2.results]})}\n\n"
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

            return StreamingResponse(_replay2(), media_type="text/event-stream")
        raise HTTPException(status_code=404, detail="Queue not found; run may have ended")

    async def _generate() -> AsyncIterator[str]:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("stream_end", "error"):
                break

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/proxy/stats")
async def proxy_stats() -> dict:
    assert _proxy
    snap = _proxy.snapshot()
    return {
        **snap,
        "bytes_total": snap["bytes_in"] + snap["bytes_out"],
        "active_connections": _proxy.active_connections,
        "total_connections": _proxy.total_connections,
        "is_running": _proxy.is_running,
        "latency_ms": _proxy.latency_ms,
    }


@app.get("/api/proxy/timeseries")
async def proxy_timeseries(seconds: float = 60.0) -> dict:
    """Return live proxy byte timeseries for the last *seconds* seconds."""
    assert _proxy
    points = _proxy.live_timeseries(seconds)
    return {"points": points, "latency_ms": _proxy.latency_ms}
