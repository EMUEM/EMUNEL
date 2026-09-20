"""Engine base class, shared context types and the circuit breaker.

Lifecycle (the operator's interface, exactly):
    await init(config)   — validate env config, build state
    await start()        — begin background work
    await process(ctx)   — one pipeline batch (frames / config / outbound)
    await feedback(m)     — an outcome event (connection result, probe, ...)
    await stop()         — release everything, flush state

Every engine runs under the manager's circuit breaker: an exception inside
process()/feedback() bypasses the engine for that batch; N consecutive
failures disable it for a cooldown. The data path never sees the exception.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import EngineEnv


# Pipeline context kinds — an engine declares which kinds it handles.
KIND_FRAMES = "frames"          # WS frame batches on a relay hop
KIND_CONFIGGEN = "configgen"    # subscription/config payload rewrite
KIND_OUTBOUND = "outbound"     # core -> destination socket dial
KIND_PROBE = "probe"           # self-play / health probe

HOPS = ("client", "internal", "core")


@dataclass
class EngineContext:
    """One unit of work flowing through the pipeline.

    For kind=frames:  frames = list[bytes] (WS message payloads). WS message
                      boundaries are soft for every EMUNEL transport (all are
                      stream protocols over WS), so stages may merge or split
                      entries as long as the concatenated byte stream is
                      preserved.
    For kind=configgen: meta["format"] in {"singbox","clash","raw"},
                      meta["body"] = str payload (decoded), meta["headers"].
    For kind=outbound: meta["host"], meta["port"], frames[0] may be absent;
                      stages may set meta["sock"] to a pre-established socket.
    """
    kind: str = KIND_FRAMES
    hop: str = "client"
    direction: str = "down"           # up = client->core, down = core->client
    conn_id: str = ""
    frames: list[bytes] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def byte_total(self) -> int:
        return sum(len(f) for f in self.frames)


@dataclass
class EngineStatus:
    enabled: bool = False          # operator/env wanted it
    active: bool = False           # it is actually in the pipeline now
    reason: str = ""               # why not active ("" when active)
    bypassed_until: float = 0.0     # circuit breaker cooldown (monotonic)
    consecutive_errors: int = 0
    started_at: float | None = None
    metrics: dict = field(default_factory=dict)


class Engine:
    """Base class for every EMUNEL engine. Subclasses set NAME/TITLE and
    override what they need; everything is optional except process() for
    pipeline engines."""

    NAME = "Base"
    TITLE = "Base engine"
    HANDLES: frozenset[str] = frozenset()   # context kinds this engine takes
    HOSTS: frozenset[str] = frozenset({"console", "core"})  # where it can run

    def __init__(self, cfg: EngineEnv, bus, state):
        self.cfg = cfg
        self.bus = bus
        self.state = state          # engines.state.EngineStateStore
        self.status = EngineStatus()
        self.log = _EngineLogger(self)

    # ---- lifecycle ----------------------------------------------------------
    async def init(self, config: dict) -> None:  # noqa: ARG002 (interface)
        pass

    async def start(self) -> None:
        pass

    async def process(self, ctx: EngineContext) -> EngineContext:
        return ctx

    async def feedback(self, metrics: dict) -> None:  # noqa: ARG002
        pass

    async def stop(self) -> None:
        pass

    # ---- helpers -------------------------------------------------------------
    def preconditions(self) -> str | None:
        """Return a human reason when the engine cannot run (missing env
        assets, no applicable channel...). None = can run."""
        return None

    def defaults(self) -> dict:
        """Params this engine runs with (for the Engine Settings page)."""
        return {}

    def snapshot_metrics(self) -> dict:
        return dict(self.status.metrics)


class _EngineLogger:
    """Tiny per-engine logger -> engine log file + bus event on error."""

    __slots__ = ("engine",)

    def __init__(self, engine: Engine):
        self.engine = engine

    def _write(self, level: str, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{level}] {self.engine.NAME}: {message}"
        try:
            self.engine.state.append_log(self.engine.NAME, line)
        except Exception:
            pass
        if level == "ERROR":
            try:
                self.engine.bus.publish("engine.error",
                                        {"name": self.engine.NAME, "error": message[:300]})
            except Exception:
                pass

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


class CircuitBreaker:
    """Per-engine failure isolation.

    trip() on exception; after `max_errors` consecutive trips the engine is
    bypassed for `cooldown` seconds; the data path always gets the original
    context back, never an exception.
    """

    def __init__(self, max_errors: int, cooldown: float):
        self.max_errors = max(1, max_errors)
        self.cooldown = max(0.0, cooldown)
        self.errors = 0
        self.bypassed_until = 0.0
        self.total_trips = 0
        self.total_disables = 0

    def on_error(self) -> None:
        self.errors += 1
        self.total_trips += 1
        if self.errors >= self.max_errors:
            self.bypassed_until = time.monotonic() + self.cooldown
            self.total_disables += 1
            self.errors = 0

    def on_success(self) -> None:
        self.errors = 0

    def open(self) -> bool:
        return time.monotonic() < self.bypassed_until

    def stats(self) -> dict:
        return {
            "consecutive_errors": self.errors,
            "bypassed_until": self.bypassed_until or None,
            "total_trips": self.total_trips,
            "total_disables": self.total_disables,
            "open": self.open(),
        }
