"""EMUNEL Health service — independent health states.

One failed subsystem must not make the whole platform appear broken:

* liveness   : the API process itself is able to serve
* readiness  : the API can do useful work (database reachable)
* database   : connectivity + latency of the console DB
* instances  : per-instance Core health (probed live, degraded if unreachable)
* manager    : the InstanceManager/driver layer
* sync       : the link-sync worker last-pass state

Every component reports independently; the dashboard renders a degraded
badge per component instead of a global failure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from ..models.instance import Instance, InstanceStatus
from .instance_manager import get_manager
from . import link_sync

logger = logging.getLogger("emunel.api.health")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def database_health(db) -> dict:
    t0 = time.monotonic()
    try:
        await db.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000, 2),
            "checked_at": _utcnow_iso(),
        }
    except Exception as exc:
        return {"status": "down", "error": str(exc)[:200], "checked_at": _utcnow_iso()}


async def instances_health(db) -> dict:
    """Probe every running/degraded instance Core; report per-instance."""
    out = {"status": "ok", "checked_at": _utcnow_iso(), "instances": {}}
    try:
        mgr = get_manager()
    except RuntimeError:
        out["status"] = "unknown"
        out["error"] = "manager not initialised"
        return out

    rows = (await db.execute(
        __import__("sqlalchemy").select(Instance).where(
            Instance.status.in_([InstanceStatus.RUNNING, InstanceStatus.DEGRADED])
        )
    )).scalars().all()

    if not rows:
        out["instances"] = {}
        return out

    healthy = 0
    for inst in rows:
        status = await mgr.status(inst.id)
        probe = status.get("core_health") or {}
        running = bool(status.get("running"))
        ok = running and status.get("healthy")
        out["instances"][inst.id] = {
            "name": inst.name,
            "running": running,
            "healthy": ok,
            "uptime": probe.get("uptime"),
            "connections": probe.get("connections"),
            "status": inst.status.value if isinstance(inst.status, InstanceStatus) else str(inst.status),
        }
        healthy += 1 if ok else 0

    if healthy == 0:
        out["status"] = "down"
    elif healthy < len(rows):
        out["status"] = "degraded"
    return out


def manager_health() -> dict:
    try:
        mgr = get_manager()
        return {
            "status": "ok",
            "active_handles": len(mgr.handles),
            "registered": len(mgr.registry.snapshot()),
            "checked_at": _utcnow_iso(),
        }
    except RuntimeError:
        return {"status": "unknown", "checked_at": _utcnow_iso()}


def sync_health() -> dict:
    last: Optional[datetime] = link_sync.sync_worker.last_run
    result = link_sync.sync_worker.last_result or {}
    status = "unknown"
    if last is not None:
        age = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds()
        status = "ok" if age < link_sync.POLL_INTERVAL * 3 else "stale"
        if result.get("degraded"):
            status = "degraded"
    return {
        "status": status,
        "last_run": last.isoformat() if last else None,
        "last_result": result,
        "interval_s": link_sync.POLL_INTERVAL,
        "checked_at": _utcnow_iso(),
    }


async def full_health(db) -> dict:
    db_h = await database_health(db)
    inst_h = await instances_health(db)
    mgr_h = manager_health()
    sync_h = sync_health()

    components = {"database": db_h, "instances": inst_h, "manager": mgr_h, "sync": sync_h}
    statuses = [c.get("status") for c in components.values()]
    if all(s == "ok" for s in statuses):
        overall = "ok"
    elif "down" in statuses:
        overall = "degraded"
    elif "degraded" in statuses or "stale" in statuses:
        overall = "degraded"
    else:
        overall = "unknown"

    return {
        "status": overall,
        "liveness": "ok",  # this handler answering proves liveness
        "readiness": "ok" if db_h["status"] == "ok" else "degraded",
        "components": components,
        "checked_at": _utcnow_iso(),
    }
