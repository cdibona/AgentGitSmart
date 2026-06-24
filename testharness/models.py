"""Pydantic models for the test harness API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RunConfig(BaseModel):
    repo_name: str
    branch: str = "master"
    target_paths: List[str]
    approaches: List[str] = ["naive", "blobless", "agentcache"]
    num_runs: int = 3


class PhaseBreakdown(BaseModel):
    clone_s: float = 0.0
    resolve_s: float = 0.0
    fetch_s: float = 0.0


class ApproachResult(BaseModel):
    approach: str
    elapsed_s: float
    bytes_proxy_in: int = 0    # client → server (git wants)
    bytes_proxy_out: int = 0   # server → client (pack data)
    bytes_proxy_total: int = 0
    objects_received: int = 0
    disk_bytes: int = 0
    file_count: int = 0
    phases: Optional[PhaseBreakdown] = None
    error: Optional[str] = None


class RunSummary(BaseModel):
    run_id: str
    created_at: str
    status: str
    repo_name: str
    branch: str
    target_paths: List[str]
    approaches: List[str]


class RunDetail(RunSummary):
    results: List[ApproachResult] = Field(default_factory=list)
    num_runs: int = 3


class SystemStatus(BaseModel):
    git_daemon: bool = False
    git_daemon_port: int = 9418
    proxy: bool = False
    proxy_port: int = 9419
    agentcache_service: bool = False
    agentcache_port: int = 8765
    repos: List[str] = Field(default_factory=list)
