"""Synergy engine — lifecycle owner of the SynergyManager loop.

Registered like any other engine (circuit breaker, hot toggles, status,
persistence for free) but with no pipeline kinds: it never touches a
single byte of payload data. Its job is exclusively to start/stop/report
the coordination cycle between Chaos, Mesh and Genetic.
"""
from __future__ import annotations

import time

from ..base import Engine
from .synergy_manager import SynergyManager


class SynergyEngine(Engine):
    NAME = "Synergy"
    TITLE = "Synergy — DpiMesh → GeneticEngine → Chaos coordination loop"
    HANDLES = frozenset()
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002 (interface)
        self.mgr: SynergyManager | None = None

    async def start(self) -> None:
        from . import peers

        self.mgr = SynergyManager(self.cfg, self.bus)
        await self.mgr.start()
        peers.register("Synergy", self)
        self.log.info("synergy loop up (every %ss)" % self.cfg.synergy_loop_sec)

    async def stop(self) -> None:
        if self.mgr is not None:
            await self.mgr.stop()
            self.mgr = None
        try:
            from . import peers
            peers.unregister("Synergy", self)
        except Exception:                              # noqa: BLE001
            pass

    async def cycle_now(self) -> dict:
        """Run one coordination cycle immediately (used by tests/status)."""
        if self.mgr is None:
            return {"ok": False, "reason": "synergy manager not running"}
        try:
            return await self.mgr.cycle()
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    def defaults(self) -> dict:
        return {
            "loop_sec": self.cfg.synergy_loop_sec,
            "cycle": "DpiMesh -> Genetic -> Chaos -> DpiMesh",
        }

    def snapshot_metrics(self) -> dict:
        if self.mgr is None:
            return {"running": False}
        out = {"running": True}
        out.update(self.mgr.status())
        out["uptime_sec"] = round(
            time.time() - self.mgr.started_at, 1) if self.mgr.started_at else 0
        return out
