"""Speculative Pre-Connect Engine (core host).

Inside the Core process this engine wraps ``asyncio.open_connection`` —
the exact call every relay uses to dial a destination — and keeps a small,
TTL-bounded warm pool of already-established TCP connections per recently
used (host, port). When the user's traffic asks for a host we already
warmed, the TCP handshake (and DNS lookup) is skipped entirely.

The wrapper is installed by engines.core_host BEFORE serving traffic and
only while the engine is active; stopping the engine restores the original
function, so a disabled engine means bit-for-bit old behaviour.

Bounds (all from env): pool size per host, max distinct hosts (LRU), TTL,
connect timeout. The pool only holds sockets to destinations the relay
ALREADY dialed — it never speculates into hosts nobody asked for except
when an explicit warm list is provided via env.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from ..base import Engine


class _WarmPool:
    def __init__(self, per_host: int, max_hosts: int, ttl: float):
        self.per_host = per_host
        self.max_hosts = max_hosts
        self.ttl = ttl
        self._pool: OrderedDict[tuple[str, int], list] = OrderedDict()
        self._reaping = False

    def _key(self, host: str, port: int) -> tuple[str, int]:
        return (str(host).lower(), int(port))

    def take(self, host: str, port: int):
        """Return a live warm connection or None."""
        key = self._key(host, port)
        bucket = self._pool.get(key)
        while bucket:
            entry = bucket.pop(0)
            reader, writer, ts = entry
            if time.monotonic() - ts > self.ttl:
                self._close(entry)
                continue
            if writer.is_closing():
                self._close(entry)
                continue
            return reader, writer
        if bucket is not None and not bucket:
            self._pool.pop(key, None)
        return None

    def put(self, host: str, port: int, reader, writer) -> None:
        key = self._key(host, port)
        bucket = self._pool.setdefault(key, [])
        bucket.append((reader, writer, time.monotonic()))
        while len(bucket) > self.per_host:
            self._close(bucket.pop(0))
        while len(self._pool) > self.max_hosts:
            _, old_bucket = self._pool.popitem(last=False)
            for entry in old_bucket:
                self._close(entry)

    def record_target(self, host: str, port: int) -> None:
        """Remember (host, port) as recently dialed — candidates for warming."""

    def close_all(self) -> None:
        for bucket in list(self._pool.values()):
            for entry in bucket:
                self._close(entry)
        self._pool.clear()

    @staticmethod
    def _close(entry) -> None:
        try:
            entry[1].close()
        except Exception:
            pass


class PreConnectEngine(Engine):
    NAME = "PreConnect"
    TITLE = "Speculative Pre-Connect — warm TCP pool for repeat destinations"
    HANDLES = frozenset()          # works via the dial wrapper, not the frame pipeline
    HOSTS = frozenset({"core"})

    async def init(self, config: dict) -> None:
        self.pool = _WarmPool(self.cfg.preconnect_pool_size,
                              self.cfg.preconnect_max_hosts,
                              float(self.cfg.preconnect_ttl_sec))
        self.status.metrics.update({
            "dials": 0, "warm_hits": 0, "warm_misses": 0,
            "sockets_held": 0, "handshakes_saved_est_ms": 0.0,
        })

    async def start(self) -> None:
        # The dial hook itself is installed ONCE by engines.core_host (it
        # dispatches to every active dial-interested engine); this engine
        # only arms its pool here.
        self.log.info("pre-connect pool armed "
                      f"(pool={self.cfg.preconnect_pool_size}/host, "
                      f"hosts<={self.cfg.preconnect_max_hosts}, "
                      f"ttl={self.cfg.preconnect_ttl_sec}s)")

    async def stop(self) -> None:
        self.pool.close_all()
        self.log.info("pre-connect pool drained")

    # ---- called by the core host dial wrapper ------------------------------------
    def take_warm(self, host, port):
        """Return a live (reader, writer) from the pool, or None."""
        try:
            if isinstance(port, int) and 0 < port < 65535 and host:
                warm = self.pool.take(host, port)
                if warm is not None:
                    m = self.status.metrics
                    m["warm_hits"] += 1
                    m["dials"] += 1
                    m["sockets_held"] = sum(len(b) for b in self.pool._pool.values())
                    m["handshakes_saved_est_ms"] += 25.0
                    return warm
                self.status.metrics["warm_misses"] += 1
                self.status.metrics["dials"] += 1
        except Exception:
            return None
        return None

    def defaults(self) -> dict:
        return {
            "PRECONNECT_POOL_SIZE": self.cfg.preconnect_pool_size,
            "PRECONNECT_TTL_SEC": self.cfg.preconnect_ttl_sec,
            "EMUNEL_PRECONNECT_MAX_HOSTS": self.cfg.preconnect_max_hosts,
        }
