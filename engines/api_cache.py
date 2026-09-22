"""TTL response cache for engine status APIs — STABILIZATION stage 4.2.

The panel polls the engines status endpoints; every poll used to rebuild
the full status (engine iteration + per-instance worker fan-out). This
module is the engines-layer answer, exactly the spec's "Map with a
timestamp":

    from .api_cache import ttl_cache_get, ttl_cache_put

    key = "engines-status:admin"
    hit = ttl_cache_get(key, ttl=5.0)
    if hit is not None:
        return hit
    payload = await _build(...)
    ttl_cache_put(key, payload)
    return payload

Properties:
  * pure dict + monotonic timestamps — zero dependencies, O(1)
  * hard entry cap (default 64) with FIFO eviction — the cache itself can
    never grow unbounded (the memory guard also clears it under pressure)
  * stores (payload, ts) tuples; a hit returns the SAME object — callers
    must treat it as read-only (FastAPI serializes it fresh per response)
  * `clear()` hook is registered with the OOM guard so soft memory
    pressure frees every cached payload immediately
"""
from __future__ import annotations

import time

_entries: dict[str, tuple[object, float]] = {}
_order: list[str] = []
MAX_ENTRIES = 64


def ttl_cache_get(key: str, ttl: float):
    """Return the cached payload or None when missing/stale."""
    hit = _entries.get(key)
    if hit is None:
        return None
    payload, ts = hit
    if (time.monotonic() - ts) > ttl:
        _entries.pop(key, None)
        try:
            _order.remove(key)
        except ValueError:
            pass
        return None
    return payload


def ttl_cache_put(key: str, payload) -> None:
    if key in _entries:
        try:
            _order.remove(key)
        except ValueError:
            pass
    _entries[key] = (payload, time.monotonic())
    _order.append(key)
    while len(_order) > MAX_ENTRIES:
        old = _order.pop(0)
        _entries.pop(old, None)


def ttl_cache_clear() -> int:
    """Drop everything (used by the OOM guard and on hot-toggle so a fresh
    status is visible immediately). Returns the number of entries freed."""
    n = len(_entries)
    _entries.clear()
    _order.clear()
    return n


def ttl_cache_stats() -> dict:
    return {"entries": len(_entries), "max_entries": MAX_ENTRIES}
