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
import gc
import time
from typing import Callable

from .base import CircuitBreaker, Engine, EngineContext, KIND_CONFIGGEN, KIND_FRAMES, KIND_OUTBOUND, KIND_PROBE
from .bus import EventBus
from .config import ENGINE_FLAG_VARS, EngineEnv, get_env
from .state import EngineStateStore

# registry name -> (class, host affinity) filled by engines/engines/__init__.py
from .engines import REGISTRY
# merged modules (STABILIZATION stage 2) — substitution + fallback tables
from .consolidated import CHILD_TO_GROUP, MERGED_MODULES

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
        # STABILIZATION: OOM guard + rotating backups (both never-crash)
        self.mem_guard = None
        self.backup = None
        self.active_order: list[str] = []

    # ---- merged-module substitution (STABILIZATION stage 2) -----------------
    def _effective_order(self) -> list[str]:
        """Pipeline order with merged modules substituted in: when a group's
        flag is on, the module takes the position of its FIRST child and
        every later child of that group is skipped as an individual engine
        (they run INSIDE the module). Merged entries at the tail of the raw
        order are only kept when their flag is on and not already placed."""
        order: list[str] = []
        placed: set[str] = set()
        for name in self.cfg.pipeline_order:
            if name in MERGED_MODULES:
                if getattr(self.cfg, MERGED_MODULES[name]["flag"], False) \
                        and name not in placed:
                    order.append(name)
                    placed.add(name)
                continue
            parent = CHILD_TO_GROUP.get(name)
            if parent is not None and getattr(
                    self.cfg, MERGED_MODULES[parent]["flag"], False):
                if parent not in placed:
                    order.append(parent)      # first child position
                    placed.add(parent)
                continue                        # child runs inside the module
            order.append(name)
        return order

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
        order = self._effective_order()
        self.active_order = list(order)
        for name in order:
            # force=True re-applies the operator's persisted hot-enable (the
            # env kill-switch EMUNEL_ENGINE_*_ENABLED=0 still wins inside)
            ok = await self._activate(name, force=bool(persisted.get(name)))
            if not ok and name in MERGED_MODULES:
                # MULTI-LAYER FALLBACK (golden rule 4): a merged module that
                # fails to start must never take the group down — the
                # legacy engines activate individually in its place.
                self.bus.publish("engine.lifecycle", {
                    "action": "merged-module-fallback", "name": name,
                })
                self.state.append_log(name,
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [WARN] {name}: "
                    "merged module failed — falling back to legacy engines")
                for child in MERGED_MODULES[name]["children"]:
                    # force=True: the merged flag expressed intent for the
                    # WHOLE group, so even default-off children run in the
                    # fallback (the env kill-switch still wins inside)
                    await self._activate(child, force=True)
                # the module's pipeline slot belongs to its children now —
                # _build_pipelines() runs after this loop and reads this list
                if name in self.active_order:
                    idx = self.active_order.index(name)
                    self.active_order[idx:idx + 1] = [
                        c for c in MERGED_MODULES[name]["children"]]
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

        # STABILIZATION stage 3+1: OOM guard (RSS watchdog with two-level
        # degradation) — every trim/degrade hook is dynamic so hot-enabled
        # engines are covered too
        try:
            from .oom_guard import MemoryGuard

            def _trim_all_caches():
                from .api_cache import ttl_cache_clear
                ttl_cache_clear()
                for e in self.engines.values():
                    c = getattr(e, "cache", None)
                    if c is not None and hasattr(c, "clear"):
                        try:
                            c.clear()
                        except Exception:
                            pass

            def _drain_all_pools():
                for e in self.engines.values():
                    p = getattr(e, "pool", None)
                    if p is not None and hasattr(p, "drain"):
                        try:
                            p.drain()
                        except Exception:
                            pass

            self.mem_guard = MemoryGuard(
                soft_mb=self.cfg.soft_mem_mb, hard_mb=self.cfg.hard_mem_mb,
                check_sec=self.cfg.mem_check_sec, bus=self.bus,
                emergency_after=self.cfg.mem_emergency_stops)
            self.mem_guard.on_trim(_trim_all_caches)
            self.mem_guard.on_degrade(_drain_all_pools)

            def _emergency_shed_engines():
                # TASK 4 (engine/UI separation, process-level form): when RSS
                # stays above the hard ceiling the engines layer sheds ITSELF
                # so the panel (UI + API) keeps serving. Cores already run as
                # separate processes; these console-host engines are the only
                # engine code sharing the panel's process. The operator
                # restarts engines from Engine Settings after fixing the
                # pressure (set_engine_enabled -> _activate(force=True)).
                async def _stop_all():
                    stopped = []
                    for name, engine in list(self.engines.items()):
                        if not engine.status.active:
                            continue
                        try:
                            await engine.stop()
                        except Exception as exc:  # stopping must never raise
                            engine.log.error(f"emergency stop failed: {exc}")
                        engine.status.active = False
                        engine.status.reason = ("memory guard emergency stop — "
                                               "re-enable from Engine Settings")
                        stopped.append(name)
                    self._build_pipelines()   # empty pipelines = raw passthrough
                    if stopped:
                        self.state.append_log("system",
                            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [WARN] memory guard: "
                            f"emergency engine stop ({', '.join(stopped)}) — "
                            "panel kept alive; re-enable from Engine Settings")
                        self.bus.publish("engine.lifecycle", {
                            "action": "emergency-stop", "names": stopped})
                    print(f"[emunel-engines] memory guard: emergency engine "
                          f"stop ({len(stopped)} engines) — panel kept alive",
                          flush=True)
                try:
                    task = asyncio.create_task(_stop_all())
                    self._tasks.append(task)   # keep a reference
                except RuntimeError:
                    pass   # no running loop (test context): skip
                gc.collect()

            self.mem_guard.on_emergency(_emergency_shed_engines)
            try:
                from .middleware import ws_active_count as _ws_count
                self.mem_guard.on_stat(lambda: {"ws_conns": _ws_count()})
            except Exception:
                pass
            await self.mem_guard.start()
        except Exception as exc:  # the guard itself must never break boot
            print(f"[emunel-engines] WARNING: memory guard not started: {exc}",
                  flush=True)

        # STABILIZATION stage 1.3: rotating state backups — console host only
        # (per-instance Core state is disposable; the console DB is not)
        if self.host == "console":
            try:
                from .persistence import BackupService
                self.backup = BackupService(
                    self.cfg.data_dir,
                    interval_h=self.cfg.backup_interval_h,
                    keep=self.cfg.backup_keep,
                    max_mb=self.cfg.backup_max_mb)
                await self.backup.start()
            except Exception as exc:
                print(f"[emunel-engines] WARNING: backup service not started: {exc}",
                      flush=True)

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
        # STABILIZATION: guard + backups go down with the manager
        if self.mem_guard is not None:
            try:
                await self.mem_guard.stop()
            except Exception:
                pass
        if self.backup is not None:
            try:
                await self.backup.stop()
            except Exception:
                pass
            self.backup = None

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
        # merged facades publish their children by NAME into this manager
        # (backward compatibility for the API routes) — they need the ref
        if hasattr(engine, "attach_manager"):
            try:
                engine.attach_manager(self)
            except Exception:
                pass
        engine.status.enabled = True
        if self.host not in cls.HOSTS:
            hint = (" — runs inside instances" if self.host == "console" else "")
            engine.status.reason = f"not applicable in {self.host} host{hint}"
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
        if name in MERGED_MODULES:
            # merged modules are strictly opt-in
            if not getattr(self.cfg, MERGED_MODULES[name]["flag"], False):
                return ("off by default (enable in Engine Settings or set "
                        f"{ENGINE_FLAG_VARS.get(name, name.upper())}=true)")
            return None
        parent = CHILD_TO_GROUP.get(name)
        if parent is not None and not self.cfg.legacy_engines_enabled:
            # legacy engines of a group are only skipped when the merged
            # module is NOT taking them over
            if not getattr(self.cfg, MERGED_MODULES[parent]["flag"], False):
                return ("legacy engines disabled (LEGACY_ENGINES_ENABLED=false) "
                        "— enable the merged module or set LEGACY_ENGINES_ENABLED=true")
        key = f"{cls.NAME.lower()}_on"
        if hasattr(self.cfg, key) and not getattr(self.cfg, key):
            return "off by default (enable in Engine Settings — the choice persists)"
        return None

    def _build_pipelines(self) -> None:
        self.pipelines = {KIND_FRAMES: [], KIND_CONFIGGEN: [], KIND_OUTBOUND: [], KIND_PROBE: []}
        for name in (self.active_order or self._effective_order()):
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
        # STABILIZATION stage 2: a child toggled while its merged module is
        # active routes INTO the module (no double instantiation)
        parent_name = CHILD_TO_GROUP.get(canonical)
        if parent_name is not None:
            parent = self.engines.get(parent_name)
            if parent is not None and parent.status.active \
                    and hasattr(parent, "enable_child"):
                if canonical in parent.children or not enabled:
                    if enabled:
                        ok, reason = await parent.enable_child(canonical)
                    else:
                        ok, reason = await parent.disable_child(canonical)
                    self._rebuild()
                    self._record_toggle(canonical, enabled)
                    self.bus.publish("engine.lifecycle", {
                        "action": "enable" if enabled else "disable",
                        "name": canonical, "inside": parent_name})
                    return ok, reason
                # child not instantiated in THIS host (core-host child from
                # the console) — fall through to the persist-for-worker path
        engine = self.engines.get(canonical)
        if enabled:
            if engine is not None and engine.status.active:
                return True, "already active"
            # instantiate through the normal path (idempotent)
            was_active = await self._activate(canonical, force=True)
            # Persist the operator's intent REGARDLESS of this host's
            # applicability: core-host engines (FEC, PreConnect, Congestion,
            # Compress) hot-enabled from the console cannot be active HERE,
            # but the worker reads this persisted toggle and launches instance
            # Cores through engines.core_host with the engine turned on.
            if not was_active and canonical in self.engines:
                reason = self.engines[canonical].status.reason or "cannot activate"
                not_applicable = "not applicable" in reason
                if not_applicable:
                    self._rebuild()
                    self._record_toggle(canonical, True)
                    self.bus.publish("engine.lifecycle",
                                      {"action": "enable-core-host", "name": canonical})
                    return True, ("enabled — runs inside instances "
                                  "(not applicable on the console host)")
                return False, reason
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
        # a toggle invalidates every cached status response instantly
        try:
            from .api_cache import ttl_cache_clear
            ttl_cache_clear()
        except Exception:
            pass
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
            entry = {
                "name": name,
                "title": engine.TITLE,
                "enabled": engine.status.enabled,
                "active": engine.status.active,
                "reason": engine.status.reason,
                "host": sorted(engine.HOSTS),
                "params": params,
                "metrics": metrics,
                "breaker": self.breakers[name].stats() if name in self.breakers else {},
            }
            # merged-module view: children published by an active facade
            # get an "inside" annotation + the module's group view
            parent_name = CHILD_TO_GROUP.get(name)
            if parent_name is not None:
                parent = self.engines.get(parent_name)
                if parent is not None and parent.status.active \
                        and getattr(parent, "children", {}).get(name) is engine:
                    entry["inside"] = parent_name
                    try:
                        entry["group"] = parent.group_status()
                    except Exception:
                        pass
            engines_out.append(entry)
        return {
            "host": self.host,
            "enabled": self.cfg.enabled,
            "pipeline_order": self.cfg.pipeline_order,
            "pipeline_effective": self.active_order or self._effective_order(),
            "pipelines": self.pipelines,
            "engines": engines_out,
            "data_dir": self.state.data_dir(),
            "volume_warning": self.state.volume_warning,
            "bus": self.bus.stats(),
            "uptime": (time.monotonic() - self._started_at) if self._started_at else 0,
            # STABILIZATION report block (stage 3/4/1 visibility)
            "stabilization": {
                "mem_guard": self.mem_guard.status() if self.mem_guard else None,
                "backup": self.backup.status() if self.backup else None,
                "legacy_engines_enabled": self.cfg.legacy_engines_enabled,
                "merged_flags": {
                    n: getattr(self.cfg, spec["flag"], False)
                    for n, spec in MERGED_MODULES.items()
                },
            },
        }

    def _ordered_names(self) -> list[str]:
        order = list(self.active_order or self._effective_order())
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
