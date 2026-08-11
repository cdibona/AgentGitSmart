"""Run one benchmark approach inside a fresh Docker container."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .metrics import CpuSampler

log = logging.getLogger(__name__)

IMAGE_TAG = "agentgitsmart-agent:bookworm"
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_DOCKERFILE_DIR = str(Path(__file__).resolve().parent / "docker")
_SENTINEL_START = "__PACK_RESULT__"
_SENTINEL_END = "__PACK_END__"
_DOCKER_AVAILABLE: Optional[bool] = None


def is_docker_available() -> bool:
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE is None:
        try:
            r = subprocess.run(
                ["docker", "version"],
                capture_output=True, timeout=5,
            )
            _DOCKER_AVAILABLE = r.returncode == 0
        except Exception:
            _DOCKER_AVAILABLE = False
    return _DOCKER_AVAILABLE


def ensure_image(log_fn: Callable[[str], None]) -> bool:
    """Build the agentgitsmart-agent Docker image if not present. Returns True on success."""
    check = subprocess.run(
        ["docker", "image", "inspect", IMAGE_TAG],
        capture_output=True,
    )
    if check.returncode == 0:
        return True
    log_fn(f"Building Docker image {IMAGE_TAG}...")
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, _DOCKERFILE_DIR],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        log_fn(f"Docker build failed: {build.stderr[-500:]}")
        return False
    log_fn(f"Docker image {IMAGE_TAG} ready.")
    return True


def _find_cgroup_cpu_stat(container_id: str) -> Optional[str]:
    """Try known cgroup v2 paths for a container's cpu.stat."""
    candidates = [
        f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope/cpu.stat",
        f"/sys/fs/cgroup/docker/{container_id}/cpu.stat",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def run_in_container(
    approach: str,
    repo_url: str,
    commit: str,
    branch: str,
    target_paths: list[str],
    service_url: str,
    symbol: str,
    cpu_interval_s: float = 0.2,
    on_cpu_sample: Optional[Callable[[float, float], None]] = None,
    use_real_agent: bool = False,
    agent_pct: float = 1.0,
    agent_seed: int = 42,
    use_bundle: bool = True,
    wait_timeout: int = 600,
) -> dict:
    """
    Run one approach in a fresh container. Returns:
      {clone, clone_ms, agent_task, cpu_samples, used_docker: True}
    Raises on infra failure.
    """
    if use_real_agent:
        cmd = [
            "docker", "run", "-d",
            "--network=host",
            "-v", f"{_REPO_ROOT}:/pack:ro",
            "-e", "PYTHONPATH=/pack",
            "-e", "GIT_TERMINAL_PROMPT=0",
            IMAGE_TAG,
            "python3", "/pack/testharness/real_agent_entry.py",
            "--approach", approach,
            "--repo-url", repo_url,
            "--commit", commit,
            "--branch", branch,
            "--service-url", service_url,
            "--pct", str(agent_pct),
            "--seed", str(agent_seed),
        ]
        if not use_bundle:
            cmd.append("--no-bundle")
    else:
        target_args = []
        for p in target_paths:
            target_args += ["--target", p]

        cmd = [
            "docker", "run", "-d",
            "--network=host",
            "-v", f"{_REPO_ROOT}:/pack:ro",
            "-e", "PYTHONPATH=/pack",
            "-e", "GIT_TERMINAL_PROMPT=0",
            IMAGE_TAG,
            "python3", "/pack/testharness/container_entry.py",
            "--approach", approach,
            "--repo-url", repo_url,
            "--commit", commit,
            "--branch", branch,
            "--service-url", service_url,
            "--symbol", symbol,
        ] + target_args

    start_proc = subprocess.run(cmd, capture_output=True, text=True)
    if start_proc.returncode != 0:
        raise RuntimeError(f"docker run failed: {start_proc.stderr}")

    container_id = start_proc.stdout.strip()
    full_id = container_id  # full 64-char ID

    sampler: Optional[CpuSampler] = None
    try:
        # Find cgroup path (give the container a moment to start)
        time.sleep(0.3)
        cgroup_path = _find_cgroup_cpu_stat(full_id)
        if cgroup_path:
            sampler = CpuSampler(
                kind="cgroup",
                cgroup_cpu_stat_path=cgroup_path,
                interval_s=cpu_interval_s,
                on_sample=on_cpu_sample,
            )
        else:
            # Fallback: get container PID and use pidtree
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Pid}}", full_id],
                capture_output=True, text=True, timeout=5,
            )
            if inspect.returncode == 0:
                try:
                    pid = int(inspect.stdout.strip())
                    if pid > 0:
                        sampler = CpuSampler(
                            kind="pidtree",
                            root_pid=pid,
                            interval_s=cpu_interval_s,
                            on_sample=on_cpu_sample,
                        )
                except ValueError:
                    pass

        if sampler:
            sampler.start()

        # Wait for container to finish (timeout 120s)
        wait_proc = subprocess.run(
            ["docker", "wait", full_id],
            capture_output=True, text=True, timeout=120,
        )
        exit_code = int(wait_proc.stdout.strip()) if wait_proc.stdout.strip().isdigit() else 1

    finally:
        if sampler:
            sampler.stop()

    # Collect logs BEFORE removing the container
    logs_proc = subprocess.run(
        ["docker", "logs", full_id],
        capture_output=True, text=True, timeout=10,
    )
    stdout = logs_proc.stdout

    # Now clean up
    subprocess.run(["docker", "rm", "-f", full_id], capture_output=True, timeout=10)

    # Extract sentinel-wrapped JSON
    start_idx = stdout.find(_SENTINEL_START)
    end_idx = stdout.find(_SENTINEL_END)
    if start_idx == -1 or end_idx == -1:
        raise RuntimeError(
            f"No result sentinel in container output (exit={exit_code}). "
            f"stdout[:500]: {stdout[:500]}"
        )
    payload_str = stdout[start_idx + len(_SENTINEL_START):end_idx]
    payload = json.loads(payload_str)

    if "error" in payload:
        raise RuntimeError(f"Container task error: {payload['error']}")

    if "result" in payload:
        payload["real_agent_data"] = payload["result"]

    payload["cpu_samples"] = sampler.samples if sampler else []
    payload["used_docker"] = True
    return payload
