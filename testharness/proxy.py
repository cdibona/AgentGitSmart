"""Transparent TCP byte-counting proxy.

Sits between test clients and git daemon, counting every byte that
flows in both directions.  Byte counts accumulate globally; callers
take a snapshot before a test phase and compute the delta after.

Also supports:
  - Artificial latency injection (set_latency / latency_ms property)
  - Per-interval byte timeseries sampling (_sample_loop, get_timeseries)

Usage::

    proxy = ByteCountingProxy("127.0.0.1", 9419, "127.0.0.1", 9418)
    await proxy.start()

    snap = proxy.snapshot()
    # ... run git clone git://127.0.0.1:9419/repo.git ...
    stats = proxy.delta(snap)
    # stats = {"bytes_in": N, "bytes_out": M, "bytes_total": N+M}

    ts = proxy.get_timeseries(snap)
    # ts = [{"t_ms": ..., "bytes_in": ..., "bytes_out": ...}, ...]

    await proxy.stop()
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

_CHUNK = 65_536
_SAMPLE_INTERVAL_S = 0.2


class ByteCountingProxy:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        target_host: str,
        target_port: int,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port

        # Cumulative counters.  Thread-safe in CPython (GIL), and we
        # only write from asyncio tasks so no additional locking needed.
        self._bytes_in = 0   # client → target (git wants, push data)
        self._bytes_out = 0  # target → client (pack data)
        self._active = 0
        self._total_connections = 0

        self._server: Optional[asyncio.AbstractServer] = None

        # Latency injection (milliseconds added per new connection)
        self._latency_ms: int = 0

        # Timeseries: periodic snapshots of cumulative counters
        self._start_monotonic: float = 0.0
        self._samples: list[dict] = []   # [{t_ms, bytes_in, bytes_out}, ...]
        self._sampler_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Latency injection
    # ------------------------------------------------------------------

    def set_latency(self, ms: int) -> None:
        """Set artificial per-connection latency in milliseconds (0 = off)."""
        self._latency_ms = max(0, int(ms))

    @property
    def latency_ms(self) -> int:
        return self._latency_ms

    # ------------------------------------------------------------------
    # Byte accounting
    # ------------------------------------------------------------------

    def _now_ms(self) -> float:
        return (time.monotonic() - self._start_monotonic) * 1000.0

    def snapshot(self) -> dict:
        """Return current cumulative byte counts (immutable snapshot)."""
        return {
            "bytes_in": self._bytes_in,
            "bytes_out": self._bytes_out,
            "t_ms": self._now_ms(),
        }

    def delta(self, before: dict) -> dict:
        """Bytes transferred since a previous snapshot."""
        b_in = self._bytes_in - before["bytes_in"]
        b_out = self._bytes_out - before["bytes_out"]
        return {
            "bytes_in": b_in,
            "bytes_out": b_out,
            "bytes_total": b_in + b_out,
        }

    def get_timeseries(self, since_snap: dict) -> list[dict]:
        """
        Return per-interval byte-delta points since *since_snap*.

        since_snap must be a snapshot dict (from snapshot()) taken before
        the operation started.  Returns a list of:
          {"t_ms": float, "bytes_in": int, "bytes_out": int}
        where t_ms is rebased to 0 at since_snap["t_ms"].
        """
        since_t = since_snap.get("t_ms", 0.0)
        since_bi = since_snap.get("bytes_in", 0)
        since_bo = since_snap.get("bytes_out", 0)

        relevant = [s for s in self._samples if s["t_ms"] > since_t]
        if not relevant:
            return []

        result = []
        prev_bi = since_bi
        prev_bo = since_bo
        for s in relevant:
            bi_delta = max(0, s["bytes_in"] - prev_bi)
            bo_delta = max(0, s["bytes_out"] - prev_bo)
            result.append({
                "t_ms": round(s["t_ms"] - since_t, 1),
                "bytes_in": bi_delta,
                "bytes_out": bo_delta,
            })
            prev_bi = s["bytes_in"]
            prev_bo = s["bytes_out"]
        return result

    def live_timeseries(self, last_seconds: float = 60.0) -> list[dict]:
        """Return the most recent *last_seconds* of timeseries samples."""
        cutoff = self._now_ms() - last_seconds * 1000.0
        return [s for s in self._samples if s["t_ms"] >= cutoff]

    @property
    def active_connections(self) -> int:
        return self._active

    @property
    def total_connections(self) -> int:
        return self._total_connections

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        counter: list,  # [int] — mutable single-element list for in-place update
    ) -> None:
        try:
            while True:
                data = await reader.read(_CHUNK)
                if not data:
                    break
                counter[0] += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        self._active += 1
        self._total_connections += 1
        bytes_in: list = [0]
        bytes_out: list = [0]

        # Inject latency before forwarding to target
        if self._latency_ms:
            await asyncio.sleep(self._latency_ms / 1000.0)

        try:
            target_reader, target_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
        except Exception as exc:
            log.warning("proxy: cannot reach target %s:%d — %s",
                        self.target_host, self.target_port, exc)
            client_writer.close()
            self._active -= 1
            return

        await asyncio.gather(
            self._pipe(client_reader, target_writer, bytes_in),
            self._pipe(target_reader, client_writer, bytes_out),
            return_exceptions=True,
        )

        self._bytes_in += bytes_in[0]
        self._bytes_out += bytes_out[0]
        self._active -= 1

    # ------------------------------------------------------------------
    # Timeseries sampler
    # ------------------------------------------------------------------

    async def _sample_loop(self) -> None:
        """Periodically snapshot cumulative counters into self._samples."""
        while True:
            await asyncio.sleep(_SAMPLE_INTERVAL_S)
            self._samples.append({
                "t_ms": self._now_ms(),
                "bytes_in": self._bytes_in,
                "bytes_out": self._bytes_out,
            })

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._start_monotonic = time.monotonic()
        self._samples = []
        self._server = await asyncio.start_server(
            self._handle,
            self.listen_host,
            self.listen_port,
        )
        self._sampler_task = asyncio.create_task(self._sample_loop())
        log.info("proxy: listening on %s:%d → %s:%d",
                 self.listen_host, self.listen_port,
                 self.target_host, self.target_port)

    async def stop(self) -> None:
        if self._sampler_task is not None:
            self._sampler_task.cancel()
            try:
                await self._sampler_task
            except asyncio.CancelledError:
                pass
            self._sampler_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            log.info("proxy: stopped")

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()
