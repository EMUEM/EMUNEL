"""TransportEngine — merged Group B (STABILIZATION stage 2.1 + 5).

  FEC + PreConnect + SessionResumption (0-RTT) + Congestion

Runs mostly inside the per-instance Core host (the dial wrapper is where
PreConnect/Congestion do their work; the console only hosts
SessionResumption). Activation simplification (spec 5.2):

  * Pre-Connect is ALWAYS part of the module when TRANSPORT_MERGED=true —
    and, unlike legacy mode, the warm pool actually gets FILLED: the dial
    hook now records every dialed destination and speculatively warms an
    extra connection in the background (bounded) for repeat targets
  * FEC's redundancy ratio is adaptive and live:
        ratio = min(0.3, max(0.05, loss * 3))
    driven by Congestion's measured loss — with loss=0 it still keeps the
    5% floor armed (the honest TCP-dormant reason stays: FEC activates on
    datagram channels, the codec + ratio stay exercised + tested)
  * warm sockets get TCP_NODELAY so the first byte after a warm hit is
    not stuck in Nagle's algorithm (spec 6.3)
  * debug logging (spec 5.3) sits behind EMUNEL_ENGINE_DEBUG=true and
    logs one line per init/start — off by default, zero overhead
"""
from __future__ import annotations

import dataclasses
import os

from ..base import EngineContext
from .facade import ConsolidatedEngine


class TransportEngine(ConsolidatedEngine):
    NAME = "Transport"
    TITLE = ("Transport & Recovery — FEC + Pre-Connect + Session Resumption "
             "+ Congestion (merged Group B)")
    GROUP = "transport"
    CHILDREN = ("PreConnect", "Congestion", "FEC", "SessionResumption")
    PIPELINE_VAR = "EMUNEL_TRANSPORT_PIPELINE"

    FEC_RATIO_FLOOR = 0.05      # spec 5.2: even with loss=0, 5% redundancy
    FEC_RATIO_CEIL = 0.30       # spec 5.2: min(0.3, ...)
    FEC_LOSS_GAIN = 3.0         # spec 5.2: max(0.05, loss * 3)

    async def init(self, config: dict) -> None:
        await super().init(config)
        self._debug = _bool_env("EMUNEL_ENGINE_DEBUG", False)

    async def start(self) -> None:
        await super().start()
        if self._debug:
            self.log.info(
                "debug: transport merged up — children=%s fec_ratio=%.3f "
                "(adaptive from measured loss)" %
                ([n for n, c in self.children.items() if c.status.active],
                 self.adaptive_fec_ratio()))
            for name, child in self.children.items():
                if child.status.active:
                    child.log.info(f"debug: {name} active inside Transport "
                                  f"(merged Group B)")

    async def process(self, ctx: EngineContext) -> EngineContext:
        # keep the FEC ratio live-adapted before dispatching the batch
        self._apply_adaptive_fec_ratio()
        return await super().process(ctx)

    # ---- adaptive FEC ratio (spec 5.2) ----------------------------------------
    def adaptive_fec_ratio(self) -> float:
        loss = 0.0
        congestion = self.children.get("Congestion")
        if congestion is not None and congestion.status.active:
            try:
                # Congestion reports a 0..100 percentage in its metrics
                loss_pct = float(congestion.status.metrics.get(
                    "loss_ewma_percent", 0.0) or 0.0)
                loss = min(1.0, max(0.0, loss_pct / 100.0))
            except (TypeError, ValueError):
                loss = 0.0
        ratio = min(self.FEC_RATIO_CEIL,
                    max(self.FEC_RATIO_FLOOR, loss * self.FEC_LOSS_GAIN))
        return ratio

    def _apply_adaptive_fec_ratio(self) -> None:
        fec = self.children.get("FEC")
        if fec is None or not fec.status.active:
            return
        ratio = self.adaptive_fec_ratio()
        try:
            fec.cfg.fec_ratio = ratio          # child cfg is a private copy
            fec.status.metrics["adaptive_ratio"] = round(ratio, 4)
        except Exception:
            pass

    # ---- per-child env -----------------------------------------------------------
    def _child_cfg(self, name: str):
        if name == "PreConnect":
            # spec 3.2: Pre-Connect cache = 50 hosts, small per-host pool,
            # spec 6.3: TCP_NODELAY on every warm socket
            return dataclasses.replace(
                self.cfg,
                preconnect_max_hosts=min(50, max(8, self.cfg.preconnect_max_hosts)),
                preconnect_pool_size=min(4, max(1, self.cfg.preconnect_pool_size)),
            )
        if name == "FEC":
            return dataclasses.replace(self.cfg, fec_ratio=self.FEC_RATIO_FLOOR)
        return self.cfg

    def defaults(self) -> dict:
        out = super().defaults()
        out["adaptive_fec_ratio"] = round(self.adaptive_fec_ratio(), 4)
        out["fec_ratio_formula"] = "min(0.30, max(0.05, loss * 3))"
        return out


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
