"""Pydantic models for the test harness API."""

from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class RunConfig(BaseModel):
    repo_name: str
    branch: str = "master"
    target_paths: List[str]
    approaches: List[str] = ["naive", "blobless", "agentcache"]
    num_runs: int = 3
    use_docker: bool = True
    latency_ms: int = 0
    use_real_agent: bool = False
    agent_pct: float = 1.0
    agent_seed: int = 42


class PhaseBreakdown(BaseModel):
    clone_s: float = 0.0
    resolve_s: float = 0.0
    fetch_s: float = 0.0


class RealAgentMetrics(BaseModel):
    agentcache_detected: bool = False
    bundle_used: bool = False
    files_found: int = 0
    files_selected: int = 0
    files_fetched: int = 0
    files_modified: int = 0
    comments_modified: int = 0
    fetch_roundtrips: int = 0
    commit_sha: Optional[str] = None
    phase_clone_ms: float = 0.0
    phase_discover_ms: float = 0.0
    phase_fetch_ms: float = 0.0
    phase_edit_ms: float = 0.0
    phase_commit_ms: float = 0.0


class TimeseriesPoint(BaseModel):
    t_ms: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    cpu_pct: float = 0.0


class AgentTaskMetrics(BaseModel):
    symbol_lookup_ms: float = 0.0
    file_read_ms: float = 0.0
    network_roundtrips: int = 0
    total_agent_ready_ms: float = 0.0
    grep_cpu_pct: float = 0.0


class ApproachResult(BaseModel):
    approach: str
    elapsed_s: float
    bytes_proxy_in: int = 0  # client → server (git wants)
    bytes_proxy_out: int = 0  # server → client (pack data)
    bytes_proxy_total: int = 0
    objects_received: int = 0
    disk_bytes: int = 0
    file_count: int = 0
    phases: Optional[PhaseBreakdown] = None
    error: Optional[str] = None
    # New Docker / agent-task fields
    clone_ms: float = 0.0
    timeseries: List[TimeseriesPoint] = Field(default_factory=list)
    agent_task: Optional[AgentTaskMetrics] = None
    used_docker: bool = False
    latency_ms: int = 0
    real_agent: Optional[RealAgentMetrics] = None


class RunSummary(BaseModel):
    run_id: str
    created_at: str
    status: str
    repo_name: str
    branch: str
    target_paths: List[str]
    approaches: List[str]
    description: Optional[str] = None  # human-readable "what was done"


class RunDetail(RunSummary):
    results: List[ApproachResult] = Field(default_factory=list)
    num_runs: int = 3
    use_docker: bool = True
    latency_ms: int = 0
    use_real_agent: bool = False
    agent_pct: float = 1.0
    agent_seed: int = 42


class ExperimentConfig(BaseModel):
    """A comprehensive multi-project campaign (cold/warm + method comparison)."""

    repos: List[str]
    methods: List[str] = ["naive", "blobless", "agentcache"]
    passes: int = 3  # agent passes per repo (1st = cold, rest = warm)
    pct: float = 2.0  # % of source files the agent edits
    seed: int = 1000
    human_commits: int = (
        0  # teammate commits to interleave (one per gap between agent passes)
    )
    hook_warms: bool = True  # does the server hook pre-warm the new commit's cache?
    warm_method: str = (
        "hook"  # how to warm: "hook" | "action" | "both" (hook_warms must be True)
    )
    use_docker: bool = True  # run each pass in a fresh disposable container (default)
    cold_bundle: bool = (
        False  # when True, cold agentcache passes are ALLOWED to use the pre-built
        # blobless bundle (production-amortised cold).  Default False = honest
        # first-visit cost (no bundle, full history through the network).
    )


class SystemStatus(BaseModel):
    git_daemon: bool = False
    git_daemon_port: int = 9418
    proxy: bool = False
    proxy_port: int = 9419
    agentcache_service: bool = False
    agentcache_port: int = 8765
    repos: List[str] = Field(default_factory=list)
    docker_available: bool = False
