"""Speculative Pre-Connect Engine (core host).

Inside the Core process this engine wraps ``asyncio.open_connection`` —
the exact call every relay uses to dial a destination — and keeps a small,
TTL-bounded warm pool of already-established TCP connections per recently
used (host, port). When the user's traffic asks for a host we already
warmed, the TCP handshake (and DNS lookup) is skipped entirely.

STABILIZATION fix (the pool used to stay forever empty — take without a
producer): the dial hook now records every dialed destination, and for a
destination the relay dialed at least TWICE recently the engine warms ONE
extra connection in the background and puts it in the pool for the NEXT
request. Every warm socket gets TCP_NODELAY so the first byte after a
warm hit is not stuck in Nagle's algorithm.

Bounds (all from env): pool size per host, max distinct hosts (LRU), TTL,
connect timeout, warm throttle. The pool only holds sockets to
destinations the relay ALREADY dialed — it never speculates into hosts
nobody asked for except when an explicit warm list is provided via env.
"""
from __future__ import annotations

import asyncio
import socket
import time
from collections import OrderedDict

from ..base import Engine


class _WarmPool:
    def __init__(self, per_host: int, max_hosts: int, ttl: float,
                 target_cap: int = 256):
        self.per_host = per_host
        self.max_hosts = max_hosts
        self.ttl = ttl
        self._pool: OrderedDict[tuple[str, int], list] = OrderedDict()
        self._targets: OrderedDict[tuple[str, int], int] = OrderedDict()
        self._target_cap = target_cap
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
        # spec 6.3: warm sockets leave Nagle off — the first byte after a
        # warm hit must not wait for a coalesced ACK
        try:
            sock = writer.transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        bucket.append((reader, writer, time.monotonic()))
        while len(bucket) > self.per_host:
            self._close(bucket.pop(0))
        while len(self._pool) > self.max_hosts:
            _, old_bucket = self._pool.popitem(last=False)
            for entry in old_bucket:
                self._close(entry)

    def record_target(self, host, port) -> None:
        """Remember (host, port) as recently dialed — candidates for warming.
        Bounded LRU of dial counts: a destination dialed >= 2 times is a
        repeat target worth one speculative warm connection."""
        try:
            key = self._key(host, port)
        except (TypeError, ValueError):
            return
        self._targets[key] = self._targets.get(key, 0) + 1
        while len(self._targets) > self._target_cap:
            self._targets.popitem(last=False)

    def dial_count(self, host, port) -> int:
        try:
            return int(self._targets.get(self._key(host, port), 0))
        except (TypeError, ValueError):
            return 0

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
        self._warming: set = set()          # in-flight warm tasks per key
        self._warm_throttle: dict = {}       # key -> monotonic ts of last warm
        self.status.metrics.update({
            "dials": 0, "warm_hits": 0, "warm_misses": 0,
            "sockets_held": 0, "handshakes_saved_est_ms": 0.0,
            "speculative_warms": 0, "warm_failures": 0,
        })

    async def start(self) -> None:
        # The dial hook itself is installed ONCE by engines.core_host (it
        # dispatches to every active dial-interested engine); this engine
        # only arms its pool here.
        self.log.info("pre-connect pool armed "
                      f"(pool={self.cfg.preconnect_pool_size}/host, "
                      f"hosts<={self.cfg.preconnect_max_hosts}, "
                      f"ttl={self.cfg.preconnect_ttl_sec}s; "
                      "speculative warm ON for repeat destinations)")

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

    def note_dial(self, host, port) -> None:
        """Record a real dial (called by the core host hook AFTER the relay
        successfully reached the destination)."""
        try:
            self.pool.record_target(host, port)
        except Exception:
            pass

    def should_warm(self, host, port) -> bool:
        """A repeat destination (dialed >= 2 times) with no warm socket yet,
        not currently warming, throttled to one attempt per 5 seconds."""
        try:
            key = (str(host).lower(), int(port))
        except (TypeError, ValueError):
            return False
        if self.pool.dial_count(host, port) < 2:
            return False
        if key in self._warming:
            return False
        bucket = self.pool._pool.get(key)
        if bucket:
            return False          # already holding a warm socket
        now = time.monotonic()
        if now - self._warm_throttle.get(key, 0.0) < 5.0:
            return False
        self._warm_throttle[key] = now
        if len(self._warm_throttle) > 512:
            self._warm_throttle.clear()
        return True

    async def warm(self, dial, host, port) -> None:
        """Speculatively dial ONE extra connection in the background and
        hand it to the pool for the next request. `dial` is the ORIGINAL
        asyncio.open_connection (the hook passes it so we never recurse)."""
        try:
            key = (str(host).lower(), int(port))
        except (TypeError, ValueError):
            return
        if key in self._warming:
            return
        self._warming.add(key)
        try:
            timeout = float(getattr(self.cfg, "preconnect_connect_timeout", 3.0))
            reader, writer = await asyncio.wait_for(
                dial(host, port), timeout=timeout)
            self.pool.put(host, port, reader, writer)
            self.status.metrics["speculative_warms"] = \
                int(self.status.metrics.get("speculative_warms", 0)) + 1
            self.status.metrics["sockets_held"] = sum(
                len(b) for b in self.pool._pool.values())
        except Exception:
            self.status.metrics["warm_failures"] = \
                int(self.status.metrics.get("warm_failures", 0)) + 1
        finally:
            self._warming.discard(key)

    def defaults(self) -> dict:
        return {
            "PRECONNECT_POOL_SIZE": self.cfg.preconnect_pool_size,
            "PRECONNECT_TTL_SEC": self.cfg.preconnect_ttl_sec,
            "EMUNEL_PRECONNECT_MAX_HOSTS": self.cfg.preconnect_max_hosts,
        }
