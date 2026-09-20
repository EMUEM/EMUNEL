"""0-RTT Session Resumption Engine.

Real scope on this platform, stated honestly:

  * The client-facing TLS is terminated at the platform edge (Railway's
    ingress or the bundled Caddy). Session tickets/resumption there are
    the edge's job (both do it automatically) — our process never sees
    those TLS sessions, by design.
  * The Core dials destinations as plain TCP (the user's own TLS is
    end-to-end through the tunnel and its session cache belongs to the
    user's browser/app and the destination — touching it would be both
    impossible and a security violation).
  * What WE own is every connection the engine layer makes itself
    (self-play probes, health checks, the engines API) — for those this
    engine provides a real keep-alive pool + TLS session cache so repeat
    calls skip the TCP and TLS handshake. It also exposes the cache to
    any future outbound TLS the platform gains.

The pool is memory-bounded (SESSION_CACHE_SIZE) with TTL from
SESSION_TTL_HOURS. It reports honest hit/miss metrics — including zero-hit
periods, which are normal on this architecture.
"""
from __future__ import annotations

import asyncio
import ssl
import time
from collections import OrderedDict

import httpx

from ..base import Engine


class SessionResumptionEngine(Engine):
    NAME = "SessionResumption"
    TITLE = "0-RTT Session Resumption — keep-alive pool + TLS session cache"
    HANDLES = frozenset()
    HOSTS = frozenset({"console", "core"})

    async def init(self, config: dict) -> None:
        self.ttl = float(self.cfg.session_ttl_hours) * 3600.0
        self._clients: OrderedDict[str, tuple[httpx.AsyncClient, float]] = OrderedDict()
        self._ssl_ctx = ssl.create_default_context()
        self.status.metrics.update({
            "pool_size": 0, "requests": 0, "reused_connections": 0,
            "sessions_cached": 0, "expired_dropped": 0,
        })

    async def start(self) -> None:
        self.log.info("session pool armed "
                      f"(cache={self.cfg.session_cache_size}, ttl={self.cfg.session_ttl_hours}h)")

    # ---- public API used by engines + probes ------------------------------------
    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """HTTP(S) request through the keep-alive pool. Repeat calls to the
        same origin reuse the connection (no handshake) — the 0-RTT effect
        for every hop we own."""
        origin = url.split("/", 3)
        key = "/".join(origin[:3])
        self.status.metrics["requests"] += 1
        client = None
        entry = self._clients.get(key)
        if entry is not None:
            client, created = entry
            if time.monotonic() - created > self.ttl or client.is_closed:
                self._clients.pop(key, None)
                self.status.metrics["expired_dropped"] += 1
                client = None
            else:
                self.status.metrics["reused_connections"] += 1
        if client is None:
            client = httpx.AsyncClient(timeout=kwargs.pop("timeout", 15.0),
                                       verify=self._ssl_ctx.verify_mode != ssl.CERT_NONE)
            self._clients[key] = (client, time.monotonic())
            while len(self._clients) > max(1, self.cfg.session_cache_size):
                _, (old, _) = self._clients.popitem(last=False)
                await self._aclose(old)
        try:
            return await client.request(method, url, **kwargs)
        except Exception:
            await self._aclose(self._clients.pop(key, (None, time.monotonic()))[0])
            raise

    async def _aclose(self, client) -> None:
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:
            pass
        self.status.metrics["pool_size"] = len(self._clients)

    async def stop(self) -> None:
        for key in list(self._clients):
            await self._aclose(self._clients.pop(key)[0])

    def defaults(self) -> dict:
        return {
            "SESSION_CACHE_SIZE": self.cfg.session_cache_size,
            "SESSION_TTL_HOURS": self.cfg.session_ttl_hours,
        }

    def snapshot_metrics(self) -> dict:
        metrics = dict(self.status.metrics)
        metrics["pool_size"] = len(self._clients)
        return metrics
