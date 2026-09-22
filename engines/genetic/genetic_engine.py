"""GeneticEngine — an evolving population of protocol parameter genomes.

Population of N genomes (default 20; spec value) where each genome is a
complete protocol parameter set. Every GENETIC_EVOLVE_INTERVAL_SEC
(default 6h) the bottom 5 are replaced by children of the top 5, bred via
Tournament selection (size 3) → single-point crossover → 5% per-parameter
mutation (crypto-grade randomness via `secrets`).

Fitness comes from DpiMesh's real outcomes when available:

    fitness = 0.5 * successRate + 0.3 * invLatency + 0.2 * invJitter

where invLatency = 1 / (1 + latency_ms / 100) and invJitter =
1 / (1 + jitter_ms / 20) — the normalised form of the spec's 1/latency and
1/jitter terms, keeping fitness inside [0, 1] (the spec's example genome
shows fitness 0.87). Without mesh data the stored fitness is kept — the
population still evolves (Test 4), just without new evidence.

Storage: SQLite (`genetic.db`) on the engine data dir (Railway volume),
bounded by GENETIC_MAX_DB_MB (default 1MB). Exceeding the cap degrades
the engine: evolve() answers with a fallback marker, the population is
kept read-only, the Core is never involved.

With GENETIC_ENGINE_ENABLED=false the engine never starts and the genomes
stay frozen.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from ..base import Engine

# (kind, choices) or (kind, min, max[, step]) — also the crossover order.
PARAM_SPEC: dict = {
    "padding": ("int", 0, 32),
    "sni_strategy": ("choice", ("fragment", "fake_sni", "combined", "rotate")),
    "transport": ("choice", ("ws", "grpc", "xhttp", "raw")),
    "cipher": ("choice", ("aes128gcm", "aes256gcm", "chacha20poly1305")),
    "fingerprint": ("choice",
                    ("chrome", "firefox", "safari", "ios", "android", "edge")),
    "mtu": ("int", 1280, 1500, 20),
    "rtt_delay": ("int", 0, 20),          # ms
    "fec_ratio": ("float", 0.0, 0.3),
    "compression": ("choice", ("none", "zlib", "brotli4")),
}
PARAM_ORDER = tuple(PARAM_SPEC)
REPLACE_COUNT = 5          # bottom N replaced by children of top N (spec)


def random_param(key: str):
    spec = PARAM_SPEC[key]
    if spec[0] == "choice":
        return secrets.choice(spec[1])
    if spec[0] == "int":
        lo, hi = spec[1], spec[2]
        step = spec[3] if len(spec) > 3 else 1
        return lo + secrets.randbelow((hi - lo) // step + 1) * step
    lo, hi = spec[1], spec[2]
    return round(lo + (secrets.randbelow(10_000) / 10_000.0) * (hi - lo), 2)


def clamp_param(key: str, value):
    spec = PARAM_SPEC[key]
    if spec[0] == "choice":
        return value if value in spec[1] else spec[1][0]
    if spec[0] == "int":
        lo, hi = int(spec[1]), int(spec[2])
        step = spec[3] if len(spec) > 3 else 1
        try:
            v = int(value)
        except (TypeError, ValueError):
            return lo
        v = max(lo, min(hi, v))
        return lo + round((v - lo) / step) * step
    lo, hi = float(spec[1]), float(spec[2])
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return round(max(lo, min(hi, v)), 2)


def fitness_value(success_rate: float, latency_ms: float,
                  jitter_ms: float) -> float:
    """0.5*success + 0.3*invLatency + 0.2*invJitter, clamped to [0, 1]."""
    success = max(0.0, min(1.0, float(success_rate or 0.0)))
    inv_lat = 1.0 / (1.0 + max(0.0, float(latency_ms or 0.0)) / 100.0)
    inv_jit = 1.0 / (1.0 + max(0.0, float(jitter_ms or 0.0)) / 20.0)
    return round(max(0.0, min(1.0, 0.5 * success + 0.3 * inv_lat
                              + 0.2 * inv_jit)), 4)


SCHEMA = """
CREATE TABLE IF NOT EXISTS genomes (
    id TEXT PRIMARY KEY,
    params TEXT NOT NULL,
    fitness REAL NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    parent_ids TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS generations (
    generation INTEGER PRIMARY KEY,
    best_fitness REAL,
    avg_fitness REAL,
    best_id TEXT,
    ts REAL
);
"""


class GeneticEngine(Engine):
    NAME = "Genetic"
    TITLE = "GeneticEngine — evolving protocol-parameter populations"
    HANDLES = frozenset()
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002 (interface)
        self.db: sqlite3.Connection | None = None
        self.degraded = False
        self.degraded_reason = ""
        self.population: list[dict] = []
        self.generation = 0
        self._timer_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None
        self._sub = None
        self._id_counter = 0
        self.status.metrics.update({
            "evolutions": 0, "mutations": 0, "crossovers": 0,
            "fitness_updates": 0, "degraded_events": 0,
        })

    async def start(self) -> None:
        from ..synergy import peers

        try:
            # consolidated mode (LearningEngine): ONE shared SQLite for
            # Mesh + Genetic (the facade owns the connection) — legacy
            # standalone mode keeps its own genetic.db
            shared = getattr(self, "shared_db", None)
            if shared is not None:
                self.db = shared
                self._db_file = ""
                self.db.executescript(SCHEMA)
                self.db.commit()
            else:
                path = Path(self.cfg.data_dir) / "genetic.db"
                self._db_file = str(path)
                self.db = sqlite3.connect(str(path), check_same_thread=False,
                                          timeout=2.0)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=NORMAL")
                self.db.executescript(SCHEMA)
                self.db.commit()
            self._load_or_seed()
        except sqlite3.Error as exc:
            self._enter_degraded(f"sqlite open failed: {exc}")
        peers.register("Genetic", self)
        # standalone wiring: fresh mesh policies refresh the fitness
        self._sub = self.bus.subscribe("mesh.policy")
        self._listener_task = asyncio.create_task(self._policy_listener())
        self._timer_task = asyncio.create_task(self._evolve_timer())
        self.log.info("population up (%d genomes, evolve every %ss)"
                      % (len(self.population),
                         self.cfg.genetic_evolve_interval_sec))

    async def stop(self) -> None:
        for task in (self._timer_task, self._listener_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._timer_task = None
        self._listener_task = None
        if self._sub is not None:
            with contextlib.suppress(Exception):
                self.bus.unsubscribe(self._sub)
            self._sub = None
        with contextlib.suppress(Exception):
            from ..synergy import peers
            peers.unregister("Genetic", self)
        if self.db is not None:
            # a shared connection belongs to the facade — never close it here
            if getattr(self, "shared_db", None) is None:
                with contextlib.suppress(sqlite3.Error):
                    self.db.close()
            self.db = None

    # ---- population store ------------------------------------------------------
    def _load_or_seed(self) -> None:
        rows = self.db.execute(
            "SELECT id, params, fitness, generation, parent_ids FROM genomes"
        ).fetchall()
        self.population = []
        for gid, params_json, fitness, generation, parents_json in rows:
            try:
                params = json.loads(params_json)
                parents = json.loads(parents_json)
            except ValueError:
                continue
            self.population.append(self._genome_row(gid, params, fitness,
                                                    generation, parents))
            self._id_counter = max(self._id_counter,
                                   int(gid[1:]) if gid[1:].isdigit() else 0)
        if self.population:
            self.generation = max(g["generation"] for g in self.population)
            return
        # seed a fresh population (generation 0)
        target = max(4, min(64, self.cfg.genetic_population_size))
        now = time.time()
        for _ in range(target):
            gid = self._next_id()
            params = {key: random_param(key) for key in PARAM_ORDER}
            row = self._genome_row(gid, params, 0.0, 0, [])
            self.population.append(row)
            self._store(row, now)
        self.generation = 0
        self._record_generation(now)

    @staticmethod
    def _genome_row(gid: str, params: dict, fitness: float, generation: int,
                    parent_ids: list) -> dict:
        return {
            "id": gid,
            **{key: params.get(key) for key in PARAM_ORDER},
            "fitness": float(fitness),
            "generation": int(generation),
            "parent_ids": list(parent_ids or []),
        }

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"g{self._id_counter:03d}"

    def _store(self, genome: dict, now: float | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO genomes (id, params, fitness, "
            "generation, parent_ids, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (genome["id"],
             json.dumps({k: genome[k] for k in PARAM_ORDER}),
             genome["fitness"], genome["generation"],
             json.dumps(genome["parent_ids"]),
             now or time.time(), now or time.time()))

    def _record_generation(self, now: float | None = None) -> None:
        fitnesses = [g["fitness"] for g in self.population] or [0.0]
        best = max(self.population, key=lambda g: g["fitness"], default=None)
        self.db.execute(
            "INSERT OR REPLACE INTO generations (generation, best_fitness, "
            "avg_fitness, best_id, ts) VALUES (?,?,?,?,?)",
            (self.generation, max(fitnesses),
             round(sum(fitnesses) / len(fitnesses), 4),
             best["id"] if best else None, now or time.time()))
        # keep the history bounded (volume budget: < 1MB total)
        self.db.execute(
            "DELETE FROM generations WHERE generation < ?",
            (self.generation - 200,))
        self.db.commit()

    # ---- background timers --------------------------------------------------------
    async def _evolve_timer(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(self.cfg.genetic_evolve_interval_sec)))
            try:
                result = self.evolve(reason="timer")
                if result.get("ok"):
                    self.log.info("evolved to generation %s"
                                  % result["generation"])
            except Exception as exc:                    # noqa: BLE001
                self.log.error(f"timer evolve failed: {exc}")

    async def _policy_listener(self) -> None:
        while True:
            try:
                event = await self.bus.next_event(self._sub, timeout=5.0)
                if event is None or event.topic != "mesh.policy":
                    continue
                self.update_fitness_from_mesh()
            except asyncio.CancelledError:
                raise
            except Exception:                          # noqa: BLE001
                await asyncio.sleep(1.0)

    # ---- evolution -------------------------------------------------------------
    def evolve(self, reason: str = "manual") -> dict:
        """Bottom-5 out, children of the top-5 in. Never raises."""
        if not self.degraded:
            self._check_volume()
        if self.degraded or self.db is None:
            return {"ok": False, "fallback": True,
                    "reason": self.degraded_reason or "engine stopped"}
        try:
            now = time.time()
            pop = sorted(self.population, key=lambda g: g["fitness"],
                         reverse=True)
            if len(pop) < 2 * REPLACE_COUNT:
                return {"ok": False, "reason": "population too small"}
            top = pop[:REPLACE_COUNT]
            doomed = pop[-REPLACE_COUNT:]
            next_gen = self.generation + 1
            children: list[dict] = []
            for _ in range(len(doomed)):
                p1 = self._tournament(top)
                p2 = self._tournament(top)
                params = self._crossover(p1, p2)
                params = self._mutate(params,
                                      self.cfg.genetic_mutation_rate)
                child = self._genome_row(self._next_id(), params, 0.0,
                                         next_gen, [p1["id"], p2["id"]])
                children.append(child)
            for row in doomed:
                self.db.execute("DELETE FROM genomes WHERE id=?",
                                (row["id"],))
            for child in children:
                self._store(child, now)
            self.population = [g for g in pop[:-REPLACE_COUNT]] + children
            self.generation = next_gen
            self._record_generation(now)
            self.status.metrics["evolutions"] = \
                int(self.status.metrics.get("evolutions", 0)) + 1
            best = self.best_genome() or {}
            self.bus.publish("genetic.generation", {
                "generation": next_gen, "reason": reason,
                "genome": {k: best.get(k) for k in
                           ("id", "transport", "cipher", "fingerprint",
                            "sni_strategy", "mtu", "padding", "rtt_delay",
                            "fec_ratio", "compression", "fitness",
                            "generation")},
            })
            return {"ok": True, "generation": next_gen, "reason": reason,
                    "replaced": [g["id"] for g in doomed],
                    "children": [c["id"] for c in children],
                    "best": best.get("id")}
        except sqlite3.Error as exc:
            self._enter_degraded(f"sqlite error during evolve: {exc}")
            return {"ok": False, "fallback": True, "reason": self.degraded_reason}

    def _tournament(self, pool: list[dict]) -> dict:
        """Tournament selection, size 3 (spec) — crypto-grade draws."""
        k = min(3, len(pool))
        contenders = [pool[i] for i in
                      [secrets.randbelow(len(pool)) for _ in range(k)]]
        return max(contenders, key=lambda g: g["fitness"])

    def _crossover(self, a: dict, b: dict) -> dict:
        """Single-point crossover over the parameter order (spec)."""
        self.status.metrics["crossovers"] = \
            int(self.status.metrics.get("crossovers", 0)) + 1
        cut = 1 + secrets.randbelow(len(PARAM_ORDER) - 1)
        params: dict = {}
        for i, key in enumerate(PARAM_ORDER):
            params[key] = a[key] if i < cut else b[key]
        return params

    def _mutate(self, params: dict, rate: float) -> dict:
        """Per-parameter mutation at `rate` (default 5%, spec)."""
        out = dict(params)
        for key in PARAM_ORDER:
            if secrets.randbelow(10_000) < int(rate * 10_000):
                out[key] = random_param(key)
                self.status.metrics["mutations"] = \
                    int(self.status.metrics.get("mutations", 0)) + 1
        return out

    # ---- fitness ---------------------------------------------------------------
    def update_fitness_from_stats(self, stats: dict) -> int:
        """Apply mesh-derived per-transport outcomes to the population.
        Returns the number of genomes updated."""
        if self.degraded or self.db is None or not stats:
            return 0
        updated = 0
        now = time.time()
        for genome in self.population:
            stat = stats.get(genome.get("transport"))
            if not stat or not stat.get("samples"):
                continue
            genome["fitness"] = fitness_value(
                stat.get("success_rate", 0.0),
                stat.get("avg_latency_ms", 0.0) or 0.0,
                stat.get("jitter_ms", 0.0) or 0.0)
            self._store(genome, now)
            updated += 1
        if updated:
            self.db.commit()
            self.status.metrics["fitness_updates"] = \
                int(self.status.metrics.get("fitness_updates", 0)) + updated
        return updated

    def update_fitness_from_mesh(self) -> int:
        """Standalone helper: pull the stats from the Mesh engine if it is
        up (synergy does this explicitly too — both paths are safe)."""
        try:
            from ..synergy import peers
            mesh = peers.get("Mesh")
            if mesh is None:
                return 0
            return self.update_fitness_from_stats(mesh.transport_stats(hours=1))
        except Exception:                              # noqa: BLE001
            return 0

    def best_genome(self) -> dict | None:
        if not self.population:
            return None
        # fitness first; ties prefer the newer generation (fresh blood
        # over stale dominance when nothing has been evaluated yet)
        return max(self.population,
                   key=lambda g: (g["fitness"], g["generation"]))

    # ---- introspection -----------------------------------------------------------
    def population_payload(self) -> list[dict]:
        return sorted(self.population, key=lambda g: g["fitness"],
                      reverse=True)

    def history(self, limit: int = 60) -> list[dict]:
        if self.db is None:
            return []
        rows = self.db.execute(
            "SELECT generation, best_fitness, avg_fitness, best_id, ts "
            "FROM generations ORDER BY generation DESC LIMIT ?",
            (max(1, min(int(limit), 200)),)).fetchall()
        return [{"generation": r[0], "best_fitness": r[1],
                 "avg_fitness": r[2], "best_id": r[3], "ts": r[4]}
                for r in reversed(rows)]

    def defaults(self) -> dict:
        return {
            "population_size": self.cfg.genetic_population_size,
            "mutation_rate": self.cfg.genetic_mutation_rate,
            "evolve_interval_sec": self.cfg.genetic_evolve_interval_sec,
            "generation": self.generation,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }

    # ---- volume guard ---------------------------------------------------------
    def _enter_degraded(self, reason: str) -> None:
        if self.degraded:
            return
        self.degraded = True
        self.degraded_reason = reason
        self.status.metrics["degraded_events"] = \
            int(self.status.metrics.get("degraded_events", 0)) + 1
        self.log.warning(
            f"DEGRADED ({reason}) — population frozen read-only; "
            f"the Core is unaffected")

    def _check_volume(self) -> None:
        try:
            # shared-db mode (LearningEngine): the facade owns the file and
            # its checkpoint/rotation — the standalone guard only applies
            # to the legacy genetic.db
            if getattr(self, "shared_db", None) is not None:
                return
            base = Path(getattr(self, "_db_file", None)
                        or Path(self.cfg.data_dir) / "genetic.db")
            if not base.exists():
                return
            # WAL mode: count the -wal sidecar too
            total = base.stat().st_size
            for suffix in ("-wal", "-shm"):
                side = base.with_name(base.name + suffix)
                if side.exists():
                    total += side.stat().st_size
            if total > self.cfg.genetic_max_db_mb * 1024 * 1024:
                self._enter_degraded("volume cap reached (GENETIC_MAX_DB_MB)")
        except OSError:
            pass
