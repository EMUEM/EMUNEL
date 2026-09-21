"""ChaosProtocol engine — Engine-lifecycle wrapper for the chaos library.

Everything is flag-gated (CHAOS_PROTOCOL_ENABLED, default false):
inactive engine = no sessions, no listeners, no configgen changes at all.

Isolation properties (Golden Rules):
  * process() touches configgen payloads ONLY when the request explicitly
    opts in via meta["chaos"]; every other batch passes through untouched.
  * The bus listener ("genetic.generation") only updates a local hint set.
  * Self-play opens a loopback TCP socket to 127.0.0.1 — nothing on the
    public data path is ever touched.
"""
from __future__ import annotations

import asyncio
import contextlib

from ..base import KIND_CONFIGGEN, Engine, EngineContext


class ChaosEngine(Engine):
    NAME = "Chaos"
    TITLE = "Chaos Protocol — shape-shifting framing (HTTP/2→WS→gRPC→QUIC-like, 30-90s)"
    HANDLES = frozenset({KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002 (interface)
        self.proto = None
        self.genome: dict | None = None
        self._listener_task: asyncio.Task | None = None
        self._sub = None
        self.status.metrics.update({
            "self_plays": 0, "probe_ok": 0, "probe_errors": 0,
            "applied_genomes": 0, "configgen_stamps": 0,
        })

    async def start(self) -> None:
        from .chaos_protocol import ChaosProtocol
        from ..synergy import peers

        self.proto = ChaosProtocol(
            self.cfg.chaos_secret,
            tick_ms=self.cfg.chaos_tick_ms,
            max_sessions=self.cfg.chaos_max_sessions,
        )
        peers.register("Chaos", self)
        # standalone wiring: GeneticEngine's generations re-shape the hints
        self._sub = self.bus.subscribe("genetic.generation")
        self._listener_task = asyncio.create_task(self._genome_listener())
        self.log.info("chaos protocol up (tick=%dms)" % self.cfg.chaos_tick_ms)

    async def stop(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
            self._listener_task = None
        if self._sub is not None:
            with contextlib.suppress(Exception):
                self.bus.unsubscribe(self._sub)
            self._sub = None
        with contextlib.suppress(Exception):
            from ..synergy import peers
            peers.unregister("Chaos", self)
        self.proto = None            # releases every session + buffer

    # ---- standalone coupling (bus) ---------------------------------------------
    async def _genome_listener(self) -> None:
        while True:
            try:
                event = await self.bus.next_event(self._sub, timeout=5.0)
                if event is not None and event.payload.get("genome"):
                    self.apply_genome(event.payload["genome"])
            except asyncio.CancelledError:
                raise
            except Exception:                          # noqa: BLE001
                await asyncio.sleep(1.0)

    # ---- synergy hooks ----------------------------------------------------------
    def apply_genome(self, genome: dict | None) -> bool:
        """GeneticEngine's best genome -> the hint set exposed to opt-in
        clients. Never throws: a bad genome is logged and ignored."""
        try:
            if genome is None:
                return False
            if not isinstance(genome, dict) or not genome.get("id"):
                self.log.warning("ignoring malformed genome payload")
                return False
            self.genome = dict(genome)
            self.status.metrics["applied_genomes"] = \
                int(self.status.metrics.get("applied_genomes", 0)) + 1
            self.log.info("applied genome %s (gen %s)" % (
                genome.get("id"), genome.get("generation")))
            return True
        except Exception as exc:                       # noqa: BLE001
            self.log.error(f"apply_genome failed: {exc}")
            return False

    async def run_probe_round(self) -> dict:
        """One loopback self-play round; publishes an outcome event for
        DpiMesh (the synergy feedback cycle). Never raises — failures are
        reported in the result dict."""
        try:
            result = await self.proto.self_play(rounds=4, payload_size=256,
                                                interval_s=0.02)
            self.status.metrics["self_plays"] = \
                int(self.status.metrics.get("self_plays", 0)) + 1
            if result.get("integrity"):
                self.status.metrics["probe_ok"] = \
                    int(self.status.metrics.get("probe_ok", 0)) + 1
            else:
                self.status.metrics["probe_errors"] = \
                    int(self.status.metrics.get("probe_errors", 0)) + 1
            self.bus.publish("chaos.outcome", {
                "engine": self.NAME,
                "ok": bool(result.get("integrity")),
                "latency_ms": result.get("rtt_avg_ms"),
                "jitter_ms": result.get("rtt_jitter_ms"),
                "frames": result.get("frames") or [],
                "transport": "chaos",
            })
            return result
        except Exception as exc:                       # noqa: BLE001
            self.log.error(f"self-play failed: {exc}")
            self.status.metrics["probe_errors"] = \
                int(self.status.metrics.get("probe_errors", 0)) + 1
            return {"integrity": False, "error": str(exc)}

    # ---- pipeline (opt-in only) ---------------------------------------------------
    async def process(self, ctx: EngineContext) -> EngineContext:
        # Golden Rule: nothing changes unless the request asked for chaos.
        if ctx.kind == KIND_CONFIGGEN and ctx.meta.get("chaos") and self.genome:
            ctx.meta["chaos_applied"] = self.hints()
            self.status.metrics["configgen_stamps"] = \
                int(self.status.metrics.get("configgen_stamps", 0)) + 1
        return ctx

    # ---- introspection ---------------------------------------------------------
    def hints(self) -> dict:
        """Genome-derived client hints (consumed by opt-in clients)."""
        g = self.genome or {}
        return {
            "genome": g.get("id"),
            "transport": g.get("transport"),
            "cipher": g.get("cipher"),
            "fingerprint": g.get("fingerprint"),
            "sni_strategy": g.get("sni_strategy"),
            "mtu": g.get("mtu"),
            "padding": g.get("padding"),
            "rtt_delay_ms": g.get("rtt_delay"),
            "fec_ratio": g.get("fec_ratio"),
            "compression": g.get("compression"),
        }

    def defaults(self) -> dict:
        return {
            "tick_ms": self.cfg.chaos_tick_ms,
            "frames": "http2, ws, grpc, quic",
            "secret_configured": self.cfg.chaos_secret != "change_me_please",
            "max_sessions": self.cfg.chaos_max_sessions,
            "active_genome": (self.genome or {}).get("id"),
        }

    def snapshot_metrics(self) -> dict:
        out = dict(self.status.metrics)
        if self.proto is not None:
            out["protocol"] = self.proto.stats()
        return out
