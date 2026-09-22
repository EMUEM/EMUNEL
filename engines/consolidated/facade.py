"""ConsolidatedEngine facade base — STABILIZATION stage 2.

One merged module per functional group wraps its legacy engines:

    TrafficShaping  = Morph + SNIEnhanced + Chaos          (+ padding in Morph)
    Transport       = FEC + PreConnect + SessionResumption + Congestion
    Learning        = Mesh + Genetic + Synergy
    Payload         = Compress + Coalesce

Design (spec 2.2 adapted to the Python codebase):

  * legacy engine CODE IS PRESERVED — the facade instantiates the very
    same classes (``engines/engines/*.py``), it does not fork them
  * the INTERNAL pipeline order is env-configurable
    (e.g. EMUNEL_TS_PIPELINE="Morph,Chaos,SNIEnhanced")
  * shared state: one SharedSQLite + one DecisionCache + one BufferPool
    (``engines.consolidated.shared``)
  * backward compatible: children are published into
    ``manager.engines`` under their own names, so every existing API route
    (/api/engines/sni/enhanced/*, /api/mesh/*, /api/genetic/*, ...) keeps
    answering exactly as before
  * multi-layer fallback (the golden rule):
        merged module process() error  -> child bypassed for the batch,
                                          next child continues
        merged module init/start error -> the manager automatically
          falls back to running the group's legacy engines individually
        everything off                 -> Core default (unchanged)
  * children with HOSTS not containing this host simply do not
    instantiate (e.g. Transport in the console only runs SessionResumption;
    FEC/PreConnect/Congestion run in the per-instance Core hosts)

The facade joins the manager pipeline at the position of its FIRST child
in DEFAULT_PIPELINE and its HANDLES is the union of the children's.
"""
from __future__ import annotations

import dataclasses
import time

from ..base import Engine, EngineContext, KIND_CONFIGGEN, KIND_FRAMES
from .shared import BufferPool, DecisionCache, SharedSQLite


class ConsolidatedEngine(Engine):
    """Base facade. Subclasses set NAME/GROUP/CHILDREN/PIPELINE_VAR and may
    override _child_cfg() to tune per-child env inside the group."""

    NAME = "Consolidated"
    TITLE = "Consolidated engine group"
    GROUP = ""                      # group id ("traffic_shaping", ...)
    CHILDREN: tuple[str, ...] = ()  # legacy engine NAMES, run order
    PIPELINE_VAR = ""               # env var for the internal order

    # set by the subclass when it wants the shared state wired
    USES_SHARED_SQLITE = False

    def __init__(self, cfg, bus, state):
        super().__init__(cfg, bus, state)
        self.children: dict[str, Engine] = {}
        self.child_order: list[str] = []
        self._shared_sql: SharedSQLite | None = None
        self.cache = DecisionCache(
            ttl=getattr(cfg, "ts_cache_ttl", 300.0),
            max_entries=getattr(cfg, "ts_cache_max", 100))
        self.pool = BufferPool()
        self._manager = None

    # ---- manager hookup (called right after construction) --------------------
    def attach_manager(self, manager) -> None:
        self._manager = manager

    # ---- internal pipeline order ----------------------------------------------
    def _resolve_order(self, env_value: str) -> list[str]:
        raw = [p.strip() for p in (env_value or "").split(",") if p.strip()]
        order = [name for name in raw if name in self.CHILDREN]
        # append any child the operator forgot to list (safe default)
        for name in self.CHILDREN:
            if name not in order:
                order.append(name)
        return order

    # ---- child construction ------------------------------------------------------
    def _child_class(self, name: str):
        from ..engines import REGISTRY
        return REGISTRY.get(name)

    def _child_cfg(self, name: str):
        """Per-child cfg — subclasses tune intervals/levels via
        dataclasses.replace (the shared EngineEnv stays untouched)."""
        return self.cfg

    def _instantiate_child(self, name: str) -> Engine | None:
        import os
        cls = self._child_class(name)
        if cls is None:
            self.log.warning(f"child {name} not in registry — skipped")
            return None
        if self.cfg.host not in cls.HOSTS:
            # not applicable in THIS host — silently skipped; its status is
            # reported through the group view
            return None
        if name in getattr(self.cfg, "explicit_off", set()):
            self.log.info(f"child {name} disabled by env kill-switch — skipped")
            return None
        try:
            child = cls(self._child_cfg(name), self.bus, self.state)
        except Exception as exc:   # a broken child never breaks the group
            self.log.warning(f"child {name} constructor failed: {exc}")
            return None
        return child

    def _before_child_start(self, name: str, child: Engine) -> None:
        """Hook for subclasses (inject shared DBs, set TCP tuning...)."""

    # ---- lifecycle --------------------------------------------------------------
    async def init(self, config: dict) -> None:  # noqa: ARG002
        if self.USES_SHARED_SQLITE:
            try:
                self._shared_sql = SharedSQLite(self.cfg.data_dir)
            except Exception as exc:
                self.log.warning(f"shared sqlite unavailable: {exc}")
                self._shared_sql = None
        env_value = ""
        if self.PIPELINE_VAR:
            import os
            env_value = os.environ.get(self.PIPELINE_VAR, "")
        self.child_order = self._resolve_order(env_value)
        # dynamic HANDLES: the union of the children's kinds, so the
        # manager's pipeline builder places the module in every pipeline
        # its children belong to (class-level default is empty)
        handles: set = set(self.HANDLES)
        for name in self.child_order:
            cls = self._child_class(name)
            if cls is not None:
                handles |= set(cls.HANDLES)
        self.HANDLES = frozenset(handles)
        self.status.metrics.update({
            "children_active": 0, "children_skipped": 0,
            "batches": 0, "child_bypasses": 0, "cache_hits": 0,
        })

    async def start(self) -> None:
        for name in self.child_order:
            child = self._instantiate_child(name)
            if child is None:
                self.status.metrics["children_skipped"] += 1
                continue
            try:
                self._before_child_start(name, child)
                await child.init({"cfg": self.cfg.__dict__})
                child._facade_inited = True
                reason = child.preconditions()
                if reason:
                    child.status.enabled = True
                    child.status.active = False
                    child.status.reason = reason
                    self.children[name] = child   # visible, honestly inactive
                    self.log.info(f"child {name} inactive: {reason}")
                    continue
                await child.start()
                child.status.enabled = True
                child.status.active = True
                child.status.reason = ""
                child.status.started_at = time.monotonic()
                self.children[name] = child
                self.log.info(f"child {name} active inside {self.NAME}")
            except Exception as exc:
                child.status.enabled = True
                child.status.active = False
                child.status.reason = f"start failed inside {self.NAME}: {exc}"
                self.children[name] = child
                self.log.warning(f"child {name} failed to start: {exc}")
        self._publish_children()
        self.status.metrics["children_active"] = sum(
            1 for c in self.children.values() if c.status.active)

    async def stop(self) -> None:
        for name, child in self.children.items():
            if child.status.active:
                try:
                    await child.stop()
                except Exception as exc:
                    self.log.warning(f"child {name} stop failed: {exc}")
                child.status.active = False
                child.status.reason = "stopped"
        self.children.clear()
        if self._shared_sql is not None:
            self._shared_sql.close()
            self._shared_sql = None

    # ---- data path ----------------------------------------------------------------
    async def process(self, ctx: EngineContext) -> EngineContext:
        self.status.metrics["batches"] += 1
        for name in self.child_order:
            child = self.children.get(name)
            if child is None or not child.status.active:
                continue
            if ctx.kind not in child.HANDLES:
                continue
            try:
                ctx = await child.process(ctx)
            except Exception as exc:
                # per-batch fallback: bypass THIS child, keep the batch
                # flowing through the rest of the group
                self.status.metrics["child_bypasses"] += 1
                child.log.error(f"process failed inside {self.NAME}, "
                                f"bypassing this batch: {exc}")
        return ctx

    async def feedback(self, metrics: dict) -> None:
        for name in self.child_order:
            child = self.children.get(name)
            if child is None or not child.status.active:
                continue
            try:
                await child.feedback(metrics)
            except Exception:
                pass

    # ---- status -----------------------------------------------------------------
    def preconditions(self) -> str | None:
        if not self.child_order:
            return "no children configured"
        return None

    # ---- operator hot-toggles routed into the module (STABILIZATION 2.2) ----
    async def enable_child(self, name: str) -> tuple[bool, str]:
        """Hot-enable a child INSIDE the merged module (Engine Settings)."""
        child = self.children.get(name)
        if child is None:
            return False, (f"not applicable in this host — runs inside "
                           f"{'instances' if self.cfg.host == 'console' else 'the console'}")
        if child.status.active:
            return True, "already active"
        try:
            if not getattr(child, "_facade_inited", False):
                await child.init({"cfg": self.cfg.__dict__})
                child._facade_inited = True
            reason = child.preconditions()
            if reason:
                child.status.reason = reason
                return False, reason
            await child.start()
            child.status.enabled = True
            child.status.active = True
            child.status.reason = ""
            child.status.started_at = time.monotonic()
            self.status.metrics["children_active"] = sum(
                1 for c in self.children.values() if c.status.active)
            self.log.info(f"child {name} hot-enabled inside {self.NAME}")
            return True, f"enabled inside {self.NAME}"
        except Exception as exc:
            child.status.reason = f"enable failed: {exc}"
            return False, str(exc)

    async def disable_child(self, name: str) -> tuple[bool, str]:
        """Hot-disable a child inside the merged module."""
        child = self.children.get(name)
        if child is None:
            return True, "not present in this host"
        if child.status.active:
            try:
                await child.stop()
            except Exception as exc:
                self.log.warning(f"child {name} stop failed: {exc}")
            child.status.active = False
            child.status.reason = "disabled by operator"
            self.status.metrics["children_active"] = sum(
                1 for c in self.children.values() if c.status.active)
        return True, "disabled"

    def defaults(self) -> dict:
        return {
            "pipeline": ",".join(self.child_order),
            "pipeline_env_var": self.PIPELINE_VAR,
            "children_active": [n for n, c in self.children.items()
                                if c.status.active],
            "shared_sqlite": self._shared_sql is not None,
        }

    def snapshot_metrics(self) -> dict:
        out = dict(self.status.metrics)
        out["cache_entries"] = len(self.cache)
        out["children_active"] = sum(
            1 for c in self.children.values() if c.status.active)
        if self._shared_sql is not None:
            out["shared_db_bytes"] = self._shared_sql.size_bytes()
        return out

    def group_status(self) -> dict:
        """Rich per-child view for /api/engines."""
        return {
            "group": self.GROUP,
            "merged": True,
            "pipeline": self.child_order,
            "children": {
                name: {
                    "active": child.status.active,
                    "reason": child.status.reason,
                    "metrics": child.snapshot_metrics(),
                }
                for name, child in self.children.items()
            },
        }

    # ---- manager integration -------------------------------------------------------
    def _publish_children(self) -> None:
        """Make every child reachable by NAME through manager.engines —
        this is the backward-compatibility hook: existing API routes that
        look up 'SNIEnhanced' / 'Mesh' / 'Genetic' keep working with the
        merged module active."""
        if self._manager is None:
            return
        for name, child in self.children.items():
            existing = self._manager.engines.get(name)
            if existing is not None and existing is not child \
                    and existing.status.active:
                continue      # legacy engine runs standalone; do not evict
            self._manager.engines[name] = child

    def unpublish_children(self) -> None:
        if self._manager is None:
            return
        for name, child in self.children.items():
            if self._manager.engines.get(name) is child:
                self._manager.engines.pop(name, None)
