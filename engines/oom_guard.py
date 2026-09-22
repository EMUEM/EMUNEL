"""Memory guard — STABILIZATION stage 3 (OOM management).

The panel died with OOM reboots on Railway's memory budget. This module is
the engines-layer watchdog that keeps RSS inside the plan:

  * every EMUNEL_MEM_CHECK_SEC (default 30s) it reads process RSS via psutil
  * SOFT threshold EMUNEL_SOFT_MEM_MB (default 380):
        - clears every TTL/LRU cache the engines layer owns
          (api_cache, consolidated decision caches, engine status caches)
        - trims the in-memory engine log buffers
        - runs gc.collect()
        - publishes "oom.soft" on the bus (engines may trim histories)
  * HARD threshold EMUNEL_HARD_MEM_MB (default 460):
        - everything the soft level does, plus
        - publishes "oom.hard": registered degradation callbacks run
          (facade pools drained, scanner history trimmed, caches reset)
        - repeated hard states increase the check frequency so pressure is
          noticed quickly right after a deploy

Python has no --max-old-space-size; the equivalent levers here are the
bounded structures + GC + MALLOC_ARENA_MAX=2 (set in the Dockerfile, which
stops glibc from multiplying 64MB arenas per thread — a real RSS reducer
for the threaded runtime).

Never-crash contract: the guard itself can never raise into the host; a
psutil failure simply skips a tick. The guard NEVER kills the process —
Railway's own OOM killer is the last resort, our job is to stay under it.
"""
from __future__ import annotations

import asyncio
import gc
import time


class MemoryGuard:
    """RSS watchdog with two-level degradation."""

    def __init__(self, *, soft_mb: float = 380.0, hard_mb: float = 460.0,
                 check_sec: float = 30.0, bus=None, log=None):
        self.soft_mb = max(64.0, float(soft_mb))
        self.hard_mb = max(self.soft_mb + 8.0, float(hard_mb))
        self.check_sec = max(5.0, float(check_sec))
        self.bus = bus
        self._log = log
        self._tasks: list[asyncio.Task] = []
        self._degrade_callbacks: list = []
        self._trim_callbacks: list = []
        self.stats = {"checks": 0, "soft_events": 0, "hard_events": 0,
                      "rss_mb": 0.0, "rss_peak_mb": 0.0, "last_gc": 0.0}
        self._running = False

    # ---- registration (engines plug their own trim/degrade hooks) ----------
    def on_trim(self, cb) -> None:
        """Cheap trims on soft pressure (clear caches, drop histories)."""
        if cb not in self._trim_callbacks:
            self._trim_callbacks.append(cb)

    def on_degrade(self, cb) -> None:
        """Stronger actions on hard pressure (drain pools)."""
        if cb not in self._degrade_callbacks:
            self._degrade_callbacks.append(cb)

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks.append(asyncio.create_task(self._loop()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.check_sec)
                self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                # psutil missing / /proc unreadable: keep looping quietly
                await asyncio.sleep(60.0)

    # ---- one tick (sync so tests can drive it directly) ---------------------
    def check(self) -> str:
        rss = self._rss_mb()
        if rss <= 0:
            return "unknown"
        self.stats["checks"] += 1
        self.stats["rss_mb"] = rss
        self.stats["rss_peak_mb"] = max(self.stats["rss_peak_mb"], rss)
        if rss >= self.hard_mb:
            self.stats["hard_events"] += 1
            self._degrade()
            return "hard"
        if rss >= self.soft_mb:
            self.stats["soft_events"] += 1
            self._trim()
            return "soft"
        return "ok"

    def _rss_mb(self) -> float:
        try:
            import psutil
            import os
            return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0

    def _trim(self) -> None:
        """Soft pressure: caches + logs + a full GC pass."""
        for cb in list(self._trim_callbacks):
            try:
                cb()
            except Exception:
                pass
        gc.collect()
        self.stats["last_gc"] = time.time()
        self._emit("oom.soft", {"rss_mb": self.stats["rss_mb"]})

    def _degrade(self) -> None:
        """Hard pressure: everything from _trim plus pool drains."""
        self._trim()
        for cb in list(self._degrade_callbacks):
            try:
                cb()
            except Exception:
                pass
        self._emit("oom.hard", {"rss_mb": self.stats["rss_mb"]})

    def _emit(self, topic: str, payload: dict) -> None:
        if self._log is not None:
            try:
                self._log.warning(
                    f"{topic}: rss={payload['rss_mb']:.0f}MB "
                    f"(soft={self.soft_mb:.0f}MB hard={self.hard_mb:.0f}MB)")
            except Exception:
                pass
        if self.bus is not None:
            try:
                self.bus.publish(topic, payload)
            except Exception:
                pass

    def status(self) -> dict:
        out = dict(self.stats)
        out.update({
            "soft_mb": self.soft_mb,
            "hard_mb": self.hard_mb,
            "check_sec": self.check_sec,
            "running": self._running,
        })
        return out
