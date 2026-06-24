"""Transparent TCP byte-counting proxy.

Sits between test clients and git daemon, counting every byte that
flows in both directions.  Byte counts accumulate globally; callers
take a snapshot before a test phase and compute the delta after.

Usage::

    proxy = ByteCountingProxy("127.0.0.1", 9419, "127.0.0.1", 9418)
    await proxy.start()

    snap = proxy.snapshot()
    # ... run git clone git://127.0.0.1:9419/repo.git ...
    stats = proxy.delta(snap)
    # stats = {"bytes_in": N, "bytes_out": M, "bytes_total": N+M}

    await proxy.stop()
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)

_CHUNK = 65_536


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

    # ------------------------------------------------------------------
    # Byte accounting
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return current cumulative byte counts (immutable snapshot)."""
        return {
            "bytes_in": self._bytes_in,
            "bytes_out": self._bytes_out,
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
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            self.listen_host,
            self.listen_port,
        )
        log.info("proxy: listening on %s:%d → %s:%d",
                 self.listen_host, self.listen_port,
                 self.target_host, self.target_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            log.info("proxy: stopped")

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()
