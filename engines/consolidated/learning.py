"""LearningEngine — merged Group C (STABILIZATION stage 2.1 + 3.2).

  DpiMesh + Genetic + Synergy   ("Living Mirror" does not exist in this
  codebase — the group wraps what is real; the adaptation is documented
  in docs/STABILIZATION.md)

Resource shape (spec 3.2 "Learning: یک SQLite مشترک. Cron هر 10 دقیقه"):
  * ONE shared SQLite (consolidated.db): Mesh's dpi_signatures/dpi_policies
    and Genetic's genomes/generations tables live side by side — the legacy
    mesh.db/genetic.db files are NOT created in merged mode
  * ONE cadence: the group's periodic work collapses to a 10-minute tick
    (mesh aggregation 5min -> 10min, synergy loop 5min -> 10min; Genetic
    keeps its 6h evolve timer — it is already 36x slower than the cron)
  * the facade's own cron: a single 10-minute ticker that WAL-checkpoints
    the shared database and trims the decision caches — one timer instead
    of per-engine maintenance
"""
from __future__ import annotations

import asyncio
import dataclasses

from .facade import ConsolidatedEngine

CRON_INTERVAL_SEC = 600.0        # spec 3.2: 10 minutes, not 5


class LearningEngine(ConsolidatedEngine):
    NAME = "Learning"
    TITLE = ("Learning & Adaptation — DpiMesh + Genetic + Synergy "
             "(merged Group C)")
    GROUP = "learning"
    CHILDREN = ("Mesh", "Genetic", "Synergy")
    PIPELINE_VAR = "EMUNEL_LEARNING_PIPELINE"
    USES_SHARED_SQLITE = True

    async def init(self, config: dict) -> None:
        await super().init(config)
        self._cron_task: asyncio.Task | None = None

    async def start(self) -> None:
        await super().start()
        self._cron_task = asyncio.create_task(self._cron_loop())

    async def stop(self) -> None:
        if self._cron_task is not None:
            self._cron_task.cancel()
            try:
                await self._cron_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cron_task = None
        await super().stop()

    # ---- the single group cron ------------------------------------------------
    async def _cron_loop(self) -> None:
        """One timer for the whole group: WAL checkpoint + cache trims."""
        while True:
            await asyncio.sleep(CRON_INTERVAL_SEC)
            try:
                if self._shared_sql is not None:
                    self._shared_sql.checkpoint()
                self.cache.clear()
                self.status.metrics["cron_ticks"] = \
                    int(self.status.metrics.get("cron_ticks", 0)) + 1
            except asyncio.CancelledError:
                raise
            except Exception:
                pass        # the cron must never take the group down

    # ---- children wiring --------------------------------------------------------
    def _before_child_start(self, name: str, child) -> None:
        # inject the ONE shared SQLite connection (children keep their own
        # schema; tables are disjoint by design)
        if name in ("Mesh", "Genetic") and self._shared_sql is not None:
            child.shared_db = self._shared_sql.db

    def _child_cfg(self, name: str):
        # spec 3.2: the Learning group runs on a 10-minute cadence
        if name == "Mesh":
            return dataclasses.replace(
                self.cfg,
                mesh_aggregate_interval_sec=max(600, self.cfg.mesh_aggregate_interval_sec))
        if name == "Synergy":
            return dataclasses.replace(
                self.cfg,
                synergy_loop_sec=max(600, self.cfg.synergy_loop_sec))
        return self.cfg

    def defaults(self) -> dict:
        out = super().defaults()
        out["cron_interval_sec"] = CRON_INTERVAL_SEC
        out["shared_db"] = (str(self._shared_sql.path)
                            if self._shared_sql else None)
        return out
