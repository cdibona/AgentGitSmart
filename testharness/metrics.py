"""CPU usage sampling and timeseries merge utilities."""
from __future__ import annotations
import threading
import time
import logging
from typing import Callable, Optional
try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

log = logging.getLogger(__name__)


class CpuSampler:
    """
    Samples CPU usage every `interval_s` seconds into a list of {t_ms, cpu_pct} dicts.
    Two kinds:
      - "cgroup": reads usage_usec from a cgroup v2 cpu.stat path (for Docker containers)
      - "pidtree": uses psutil to sum a process tree's cpu_percent (in-process fallback)
    """
    def __init__(self, kind: str, *, cgroup_cpu_stat_path: Optional[str] = None,
                 root_pid: Optional[int] = None, interval_s: float = 0.2,
                 on_sample: Optional[Callable[[float, float], None]] = None) -> None:
        assert kind in ("cgroup", "pidtree")
        self._kind = kind
        self._cgroup_path = cgroup_cpu_stat_path
        self._root_pid = root_pid
        self._interval = interval_s
        self._on_sample = on_sample
        self._samples: list[dict] = []
        self._t0: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_ev = threading.Event()

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_ev.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    @property
    def samples(self) -> list[dict]:
        return list(self._samples)

    @property
    def peak_pct(self) -> float:
        return max((s["cpu_pct"] for s in self._samples), default=0.0)

    def _now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def _read_cgroup_usec(self) -> Optional[int]:
        try:
            with open(self._cgroup_path) as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        except Exception:
            pass
        return None

    def _loop(self) -> None:
        if self._kind == "cgroup":
            self._loop_cgroup()
        else:
            self._loop_pidtree()

    def _loop_cgroup(self) -> None:
        prev_usec: Optional[int] = None
        prev_wall: float = time.monotonic()
        while not self._stop_ev.wait(self._interval):
            usec = self._read_cgroup_usec()
            if usec is None:
                break  # container gone
            now = time.monotonic()
            if prev_usec is not None:
                delta_cpu = (usec - prev_usec) / 1e6
                delta_wall = now - prev_wall
                pct = (delta_cpu / delta_wall * 100.0) if delta_wall > 0 else 0.0
                entry = {"t_ms": self._now_ms(), "cpu_pct": round(pct, 1)}
                self._samples.append(entry)
                if self._on_sample:
                    try:
                        self._on_sample(entry["t_ms"], entry["cpu_pct"])
                    except Exception:
                        pass
            prev_usec = usec
            prev_wall = now

    def _loop_pidtree(self) -> None:
        if psutil is None:
            return
        try:
            proc = psutil.Process(self._root_pid)
            proc.cpu_percent()  # prime baseline
        except Exception:
            return
        while not self._stop_ev.wait(self._interval):
            try:
                pct = proc.cpu_percent()
                try:
                    for child in proc.children(recursive=True):
                        try:
                            pct += child.cpu_percent()
                        except psutil.NoSuchProcess:
                            pass
                except psutil.NoSuchProcess:
                    pass
                entry = {"t_ms": self._now_ms(), "cpu_pct": round(pct, 1)}
                self._samples.append(entry)
                if self._on_sample:
                    try:
                        self._on_sample(entry["t_ms"], entry["cpu_pct"])
                    except Exception:
                        pass
            except psutil.NoSuchProcess:
                break
            except Exception as e:
                log.debug("CpuSampler error: %s", e)


def merge_timeseries(byte_points: list[dict], cpu_points: list[dict]) -> list[dict]:
    """
    Merge proxy byte-delta points and CPU sample points into a unified series.
    Each output point: {t_ms, bytes_in, bytes_out, cpu_pct}
    Uses two-pointer nearest-neighbour join (±150ms window).
    If byte_points empty, falls back to cpu grid with bytes=0.
    """
    if not byte_points and not cpu_points:
        return []
    if not byte_points:
        return [{"t_ms": p["t_ms"], "bytes_in": 0, "bytes_out": 0, "cpu_pct": p["cpu_pct"]} for p in cpu_points]

    result = []
    j = 0
    for bp in byte_points:
        t = bp["t_ms"]
        # advance j to nearest cpu point
        while j + 1 < len(cpu_points) and abs(cpu_points[j+1]["t_ms"] - t) < abs(cpu_points[j]["t_ms"] - t):
            j += 1
        cpu = 0.0
        if cpu_points and abs(cpu_points[j]["t_ms"] - t) <= 150:
            cpu = cpu_points[j]["cpu_pct"]
        result.append({"t_ms": round(t, 1), "bytes_in": bp["bytes_in"], "bytes_out": bp["bytes_out"], "cpu_pct": cpu})
    return result
