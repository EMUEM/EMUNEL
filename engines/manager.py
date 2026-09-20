"""EngineManager — registry, pipeline assembly, lifecycle and status.

One manager instance per process (console host / core host). It:

  * discovers engines from engines/engines/__init__.py (explicit registry)
  * activates those that (a) appear in PIPELINE_ORDER, (b) are enabled via
    EMUNEL_ENGINE_<NAME>_ENABLED, (c) can run in this host, and (d) satisfy
    their own preconditions (missing env assets -> inactive with reason)
  * runs every stage under a circuit breaker: failures bypass the engine
    for the batch, N consecutive failures disable it for a cooldown
  * exposes hot enable/disable + status for the /api/engines/* routes
  * persists state and emits metrics on the bus
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from .base import CircuitBreaker, Engine, EngineContext, KIND_CONFIGGEN, KIND_FRAMES, KIND_OUTBOUND, KIND_PROBE
from .bus import EventBus
from .config import ENGINE_FLAG_VARS, EngineEnv, get_env
from .state import EngineStateStore

# registry name -> (class, host affinity) filled by engines/engines/__init__.py
from .engines import REGISTRY

# State-store key holding the operator's hot-toggle decisions. Engines enabled
# or disabled from the panel survive process restarts (Railway redeploys and
# crash-restarts) — this is what previously reset Morph back to "off by
# default" minutes after the operator turned it on.
TOGGLES_KEY = "EngineToggles"


class EngineManager:
    def __init__(self, host: str = "console", cfg: EngineEnv | None = None,
                 bus: EventBus | None = None, state: EngineStateStore | None = None):
        self.host = host
        self.cfg = cfg or get_env(host)
        self.bus = bus or EventBus()
        self.state = state or EngineStateStore(self.cfg.data_dir)
        self.engines: dict[str, Engine] = {}
        self.breakers: dict[str, CircuitBreaker] = {}
        self.pipelines: dict[str, list[str]] = {}     # ctx kind -> engine names
        self._tasks: list[asyncio.Task] = []
        self._started_at: float | None = None
        self._running = False
        self._op_limiter = _OpsLimiter(self.cfg.max_ops_per_sec)
        self._toggle_hooks: list[Callable] = []

    # ---- lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        if self.state.volume_warning:
            print(f"[emunel-engines] WARNING: {self.state.volume_warning}", flush=True)
        if not self.cfg.enabled:
            self.bus.publish("engine.lifecycle", {"action": "system-disabled-by-env"})
            return

        self.state.load()
        persisted = self._persisted_toggles()
        order = [n for n in self.cfg.pipeline_order]
        for name in order:
            # force=True re-applies the operator's persisted hot-enable (the
            # env kill-switch EMUNEL_ENGINE_*_ENABLED=0 still wins inside)
            await self._activate(name, force=bool(persisted.get(name)))
        # A persisted disable must also hold when the engine's DEFAULT is on
        # (e.g. Coalesce) — otherwise a restart would silently revert the
        # operator's choice.
        for name, want in persisted.items():
            if want is not False:
                continue
            engine = self.engines.get(name)
            if engine is None:
                continue
            if engine.status.active:
                try:
                    await engine.stop()
                except Exception as exc:  # stopping must never raise
                    engine.log.error(f"stop failed: {exc}")
            engine.status.active = False
            engine.status.enabled = False
            engine.status.reason = "disabled by operator (persisted)"
        self._build_pipelines()

        # background: state flush + metrics tick (never crash the host)
        self._tasks.append(asyncio.create_task(self._maintenance_loop()))
        self.bus.publish("engine.lifecycle", {
            "action": "started", "host": self.host,
            "active": self.active_engine_names(),
        })

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        for engine in self.engines.values():
            if engine.status.active:
                try:
                    await engine.stop()
                except Exception as exc:  # stopping must never raise
                    engine.log.error(f"stop failed: {exc}")
                engine.status.active = False
                engine.status.reason = "stopped"
        self.state.maybe_flush(force=True)
        self.bus.publish("engine.lifecycle", {"action": "stopped", "host": self.host})

    async def _activate(self, name: str, force: bool = False) -> bool:
        """Instantiate + start an engine, or record why it can't run.

        force=True (hot enable from the panel) overrides default-off flags
        but never an explicit env kill-switch (EMUNEL_ENGINE_*_ENABLED=0)."""
        cls = REGISTRY.get(name) or REGISTRY.get(name.title()) or REGISTRY.get(name.lower())
        if cls is None:
            self.bus.publish("engine.lifecycle", {"action": "unknown-engine", "name": name})
            return False
        try:
            engine = cls(self.cfg, self.bus, self.state)
        except Exception as exc:
            self.bus.publish("engine.error", {"name": name, "error": f"init failed: {exc}"})
            return False
        engine.status.enabled = True
        if self.host not in cls.HOSTS:
            engine.status.reason = f"not applicable in {self.host} host"
            self.engines[name] = engine
            return False
        reason = self._env_disabled_reason(cls, name, force=force)
        if reason:
            engine.status.reason = reason
            self.engines[name] = engine
            return False
        try:
            await engine.init({"cfg": self.cfg.__dict__})
        except Exception as exc:
            engine.status.reason = f"init failed: {exc}"
            self.engines[name] = engine
            self.bus.publish("engine.error", {"name": name, "error": engine.status.reason})
            return False
        reason = engine.preconditions()
        if reason:
            engine.status.reason = reason
            self.engines[name] = engine
            return False
        try:
            await engine.start()
        except Exception as exc:
            engine.status.reason = f"start failed: {exc}"
            engine.status.active = False
            self.engines[name] = engine
            self.bus.publish("engine.error", {"name": name, "error": engine.status.reason})
            return False
        engine.status.active = True
        engine.status.reason = ""
        engine.status.started_at = time.monotonic()
        self.engines[name] = engine
        self.breakers[name] = CircuitBreaker(self.cfg.bypass_errors, self.cfg.bypass_cooldown_sec)
        return True

    def _env_disabled_reason(self, cls: type[Engine], name: str,
                              force: bool = False) -> str | None:
        # explicit =0 in the environment is a hard kill-switch (panel cannot
        # override); a missing flag is just the default (panel may toggle)
        if name in self.cfg.explicit_off:
            var = ENGINE_FLAG_VARS.get(name, f"EMUNEL_ENGINE_{name.upper()}_ENABLED")
            return f"disabled by env ({var}=0)"
        if force:
            return None
        key = f"{cls.NAME.lower()}_on"
        if hasattr(self.cfg, key) and not getattr(self.cfg, key):
            return "off by default (enable in Engine Settings — the choice persists)"
        return None

    def _build_pipelines(self) -> None:
        self.pipelines = {KIND_FRAMES: [], KIND_CONFIGGEN: [], KIND_OUTBOUND: [], KIND_PROBE: []}
        for name in self.cfg.pipeline_order:
            engine = self.engines.get(name)
            if engine is None or not engine.status.active:
                continue
            for kind in engine.HANDLES:
                if kind in self.pipelines:
                    self.pipelines[kind].append(name)

    # ---- data path ---------------------------------------------------------------
    async def run_pipeline(self, ctx: EngineContext) -> EngineContext:
        """Run ctx through the pipeline for its kind. NEVER raises: a failing
        engine is bypassed (ctx continues from its last good state)."""
        stages = self.pipelines.get(ctx.kind)
        if not stages:
            return ctx
        if not self._op_limiter.allow():
            # global ops cap reached: run untransformed (platform first)
            return ctx
        for name in stages:
            engine = self.engines.get(name)
            if engine is None or not engine.status.active:
                continue
            breaker = self.breakers.get(name)
            if breaker is not None and breaker.open():
                continue
            try:
                ctx = await engine.process(ctx)
                if breaker is not None:
                    breaker.on_success()
            except Exception as exc:
                if breaker is not None:
                    breaker.on_error()
                engine.log.error(f"process failed, bypassing this batch: {exc}")
                engine.status.metrics["bypasses"] = \
                    int(engine.status.metrics.get("bypasses", 0)) + 1
                if breaker is not None and breaker.open():
                    engine.log.warning(
                        f"disabled for {self.cfg.bypass_cooldown_sec:.0f}s after repeated failures")
                    self.bus.publish("engine.lifecycle", {
                        "action": "bypass-cooldown", "name": name,
                    })
        return ctx

    # ---- feedback --------------------------------------------------------------------
    async def dispatch_feedback(self, payload: dict) -> None:
        """Route an outcome (connection result, probe result) to engines that
        declared interest — feedback never affects the data path."""
        for name, engine in self.engines.items():
            if not engine.status.active:
                continue
            try:
                await engine.feedback(payload)
            except Exception as exc:
                engine.log.error(f"feedback failed: {exc}")

    # ---- hot toggles (Engine Settings page) ----------------------------------------------
    async def set_engine_enabled(self, name: str, enabled: bool) -> tuple[bool, str]:
        canonical = self._canonical(name)
        if canonical is None:
            return False, "unknown engine"
        engine = self.engines.get(canonical)
        if enabled:
            if engine is not None and engine.status.active:
                return True, "already active"
            # instantiate through the normal path (idempotent)
            was_active = await self._activate(canonical, force=True)
            if not was_active and canonical in self.engines:
                return False, self.engines[canonical].status.reason or "cannot activate"
            self._rebuild()
            self._record_toggle(canonical, True)
            self.bus.publish("engine.lifecycle", {"action": "enable", "name": canonical})
            return True, "enabled"
        if engine is None or not engine.status.active:
            if engine is not None:
                engine.status.enabled = False
                engine.status.reason = "disabled by operator"
            self._rebuild()
            self._record_toggle(canonical, False)
            self.bus.publish("engine.lifecycle", {"action": "disable", "name": canonical})
            return True, "disabled"
        try:
            await engine.stop()
        except Exception as exc:
            engine.log.error(f"stop failed during disable: {exc}")
        engine.status.active = False
        engine.status.enabled = False
        engine.status.reason = "disabled by operator"
        self._rebuild()
        self._record_toggle(canonical, False)
        self.bus.publish("engine.lifecycle", {"action": "disable", "name": canonical})
        return True, "disabled"

    # ---- toggle persistence -------------------------------------------------------------
    def _record_toggle(self, name: str, enabled: bool) -> None:
        """Remember the operator's decision so the next boot re-applies it."""

        def _apply(payload: dict) -> None:
            payload.setdefault("enabled", {})[name] = bool(enabled)

        self.state.mutate(TOGGLES_KEY, _apply)
        self.state.maybe_flush(force=True)

    def _persisted_toggles(self) -> dict[str, bool]:
        """Previously recorded operator decisions: {engine name: enabled?}."""
        try:
            payload = self.state.get(TOGGLES_KEY, {}) or {}
            enabled = payload.get("enabled")
            if isinstance(enabled, dict):
                return {str(k): bool(v) for k, v in enabled.items()
                        if isinstance(v, bool)}
        except Exception:
            pass
        return {}

    def _canonical(self, name: str) -> str | None:
        if name in REGISTRY:
            return name
        for key in REGISTRY:
            if key.lower() == name.lower():
                return key
        return None

    def _rebuild(self) -> None:
        self._build_pipelines()
        for hook in self._toggle_hooks:
            try:
                hook()
            except Exception:
                pass

    def on_toggle(self, hook: Callable) -> None:
        self._toggle_hooks.append(hook)

    # ---- background -------------------------------------------------------------
    async def _maintenance_loop(self) -> None:
        """State flush + periodic metrics events. Never raises."""
        while True:
            try:
                await asyncio.sleep(self.cfg.status_cache_sec)
                self.state.maybe_flush()
                self.bus.publish("metrics.tick", {
                    "host": self.host,
                    "active": self.active_engine_names(),
                    "bus": self.bus.stats(),
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)

    # ---- status --------------------------------------------------------------------
    def active_engine_names(self) -> list[str]:
        return [n for n, e in self.engines.items() if e.status.active]

    def status(self) -> dict:
        engines_out = []
        for name in self._ordered_names():
            engine = self.engines.get(name)
            if engine is None:
                # registered but not instantiated (not in pipeline order)
                cls = REGISTRY.get(name)
                engines_out.append({
                    "name": name, "title": getattr(cls, "TITLE", name),
                    "enabled": False, "active": False,
                    "reason": "not in EMUNEL_PIPELINE_ORDER",
                    "host": sorted(getattr(cls, "HOSTS", ["console", "core"])),
                    "params": {}, "metrics": {}, "breaker": {},
                })
                continue
            try:
                params = engine.defaults()
            except Exception:
                params = {}
            try:
                metrics = engine.snapshot_metrics()
            except Exception:
                metrics = dict(engine.status.metrics)
            engines_out.append({
                "name": name,
                "title": engine.TITLE,
                "enabled": engine.status.enabled,
                "active": engine.status.active,
                "reason": engine.status.reason,
                "host": sorted(engine.HOSTS),
                "params": params,
                "metrics": metrics,
                "breaker": self.breakers[name].stats() if name in self.breakers else {},
            })
        return {
            "host": self.host,
            "enabled": self.cfg.enabled,
            "pipeline_order": self.cfg.pipeline_order,
            "pipelines": self.pipelines,
            "engines": engines_out,
            "data_dir": self.state.data_dir(),
            "volume_warning": self.state.volume_warning,
            "bus": self.bus.stats(),
            "uptime": (time.monotonic() - self._started_at) if self._started_at else 0,
        }

    def _ordered_names(self) -> list[str]:
        order = [n for n in self.cfg.pipeline_order]
        for name in REGISTRY:
            if name not in order:
                order.append(name)
        return order

    def engine_logs(self, name: str, limit: int = 80) -> list[str]:
        return self.state.engine_logs(name, limit)


class _OpsLimiter:
    """Whole-pipeline ops/sec ceiling (EMUNEL_ENGINE_MAX_OPS_PER_SEC).

    Userspace guard only: when exceeded, batches pass through untransformed
    so the relay is never delayed by engine bookkeeping."""

    def __init__(self, ops_per_sec: int):
        self.capacity = max(1, ops_per_sec)
        self.tokens = float(self.capacity)
        self.last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(float(self.capacity), self.tokens + (now - self.last) * self.capacity)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False
