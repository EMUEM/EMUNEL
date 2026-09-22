"""TrafficShapingEngine — merged Group A (STABILIZATION stage 2.1).

  Morph + SNIEnhanced + Chaos  (+ padding/jitter inside Morph's profiles)

Console-host group: Morph rewrites frames + configgen payloads by ISP
profile (LinUCB bandit), SNIEnhanced owns the advanced SNI techniques and
the scanner, Chaos shape-shifts the transport signature every 30-90s.

Resource shape (spec 3.2): one decision cache, TTL 300s / max 100 entries,
shared SQLite journal for warm-starting the cache after restarts — the
subscription-feed path (same body, polled repeatedly) skips the whole
group on a cache hit.
"""
from __future__ import annotations

import hashlib
import os

from ..base import KIND_CONFIGGEN, EngineContext
from .facade import ConsolidatedEngine


class TrafficShapingEngine(ConsolidatedEngine):
    NAME = "TrafficShaping"
    TITLE = ("Traffic Shaping — Morph + SNI Enhanced + Chaos in one module "
             "(merged Group A)")
    GROUP = "traffic_shaping"
    CHILDREN = ("Morph", "SNIEnhanced", "Chaos")
    PIPELINE_VAR = "EMUNEL_TS_PIPELINE"
    USES_SHARED_SQLITE = True

    # configgen response cache (subscription feeds: same body polled often)
    CACHE_ENABLED_DEFAULT = True

    async def init(self, config: dict) -> None:
        await super().init(config)
        self._cache_enabled = _bool_env("EMUNEL_TS_CACHE", self.CACHE_ENABLED_DEFAULT)
        # warm-start the decision cache from the shared journal
        if self._shared_sql is not None:
            try:
                for key in self._shared_sql.keys("ts-decisions"):
                    value = self._shared_sql.get("ts-decisions", key)
                    if value:
                        self.cache.put(key, value)
            except Exception:
                pass

    async def process(self, ctx: EngineContext) -> EngineContext:
        # configgen cache: identical (format, body) inside the TTL window
        # skips Morph's bandit round-trip + Chaos's opt injection entirely
        if self._cache_enabled and ctx.kind == KIND_CONFIGGEN:
            body = str(ctx.meta.get("body") or "")
            key = None
            if body:
                digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]
                key = f"cg:{ctx.meta.get('format', '')}:{digest}"
                hit = self.cache.get(key)
                if hit is not None and isinstance(hit, dict):
                    self.status.metrics["cache_hits"] = \
                        int(self.status.metrics.get("cache_hits", 0)) + 1
                    ctx.meta["body"] = hit.get("body", body)
                    ctx.frames = [hit.get("body", body).encode("utf-8", "replace")]
                    return ctx
            ctx = await super().process(ctx)
            if key is not None:
                out_body = str(ctx.meta.get("body") or "")
                if out_body:
                    self.cache.put(key, {"body": out_body})
                    if self._shared_sql is not None:
                        try:
                            self._shared_sql.set("ts-decisions", key, out_body)
                        except Exception:
                            pass
            return ctx
        return await super().process(ctx)

    def defaults(self) -> dict:
        out = super().defaults()
        out["cache"] = self._cache_enabled
        out["cache_ttl_s"] = self.cache.ttl
        out["cache_max"] = self.cache.max_entries
        return out


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
