"""SynergyManager — the coordination loop between the three engines.

Cycle (operator spec):
    DpiMesh -> GeneticEngine -> ChaosProtocol -> feedback -> DpiMesh

    1. DpiMesh's real outcome stats refresh every genome's fitness
    2. GeneticEngine's best genome is applied to ChaosProtocol
    3. ChaosProtocol runs one loopback probe; the outcome flows back into
       DpiMesh through the bus (mesh.outcome listener)

Every step is individually wrapped: an exception in one engine is
recorded and the rest continue (operator spec: "if one errors, the rest
keep running"). The loop itself never raises into the host.

With SYNERGY_ENABLED=false this loop does not run — the three engines
still work standalone through EventBus events (mesh.policy ->
fitness refresh, genetic.generation -> genome application,
chaos.outcome -> mesh ingest).
"""
from __future__ import annotations

import asyncio
import contextlib
import time


class SynergyManager:
    def __init__(self, cfg, bus=None):
        self.cfg = cfg
        self.bus = bus
        self._task: asyncio.Task | None = None
        self.cycles = 0
        self.started_at: float | None = None
        self.last_cycle_at: float | None = None
        self.last_results: dict = {}
        self.last_errors: dict[str, str] = {}
        self.errors_total = 0

    # ---- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self.started_at = time.time()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(self.cfg.synergy_loop_sec)))
            with contextlib.suppress(Exception):      # never kills the host
                await self.cycle()

    # ---- the coordination cycle --------------------------------------------------
    async def cycle(self) -> dict:
        from . import peers

        mesh = peers.get("Mesh")
        genetic = peers.get("Genetic")
        chaos = peers.get("Chaos")
        results: dict = {"engines": {
            "mesh": mesh is not None, "genetic": genetic is not None,
            "chaos": chaos is not None}}
        self.last_errors = {}

        # 1. DpiMesh -> GeneticEngine (fitness from real outcomes)
        if genetic is not None:
            try:
                if mesh is not None:
                    results["fitness_updated"] = genetic.update_fitness_from_stats(
                        mesh.transport_stats(hours=1))
                else:
                    results["fitness_updated"] = 0
            except Exception as exc:                  # noqa: BLE001
                self._err("genetic.fitness", exc)

        # 2. GeneticEngine -> ChaosProtocol (execute the best genome)
        if chaos is not None:
            try:
                genome = genetic.best_genome() if genetic is not None else None
                results["genome_applied"] = chaos.apply_genome(genome)
            except Exception as exc:                  # noqa: BLE001
                self._err("chaos.apply", exc)

        # 3. ChaosProtocol -> probe -> (bus) -> DpiMesh
        if chaos is not None:
            try:
                probe = await chaos.run_probe_round()
                results["probe_integrity"] = bool(probe.get("integrity"))
            except Exception as exc:                  # noqa: BLE001
                self._err("chaos.probe", exc)

        self.cycles += 1
        self.last_cycle_at = time.time()
        self.last_results = results
        return results

    def _err(self, where: str, exc: Exception) -> None:
        self.last_errors[where] = str(exc)[:200]
        self.errors_total += 1

    # ---- introspection ---------------------------------------------------------
    def status(self) -> dict:
        from . import peers

        mesh = peers.get("Mesh")
        genetic = peers.get("Genetic")
        chaos = peers.get("Chaos")
        return {
            "loop_sec": self.cfg.synergy_loop_sec,
            "cycles": self.cycles,
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "last_results": self.last_results,
            "last_errors": self.last_errors,
            "errors_total": self.errors_total,
            "engines_present": {
                "mesh": mesh is not None,
                "genetic": genetic is not None,
                "chaos": chaos is not None,
            },
        }
