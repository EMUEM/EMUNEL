"""EMUNEL API — Traffic router (real data only).

Sources: Link.used_bytes (synced from the Cores by the poller) plus live
per-instance Core stats. No simulated or estimated values; when no data
exists, zero/empty states are returned honestly.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.instance import Instance, InstanceStatus, Link
from ...models.subscription import Subscription
from ...models.user import User
from ...services.auth import get_current_user
from ...services.core_client import CoreClient, CoreUnavailable

router = APIRouter()


@router.get("/summary")
async def traffic_summary(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Platform traffic totals from synced link counters + live cores."""
    link_tot = (await db.execute(
        select(func.coalesce(func.sum(Link.used_bytes), 0), func.count(Link.id))
    )).one()
    sub_tot = (await db.execute(
        select(
            func.coalesce(func.sum(Subscription.traffic_used_bytes), 0),
            func.count(Subscription.id),
        )
    )).one()

    live_connections = 0
    live_bytes_rate = None
    running = (await db.execute(
        select(Instance).where(Instance.status == InstanceStatus.RUNNING)
    )).scalars().all()
    live_ok = 0
    for inst in running:
        try:
            stats = await CoreClient(inst.core_port, inst.core_api_token).stats()
            live_connections += int(stats.get("active_connections") or 0)
            live_ok += 1
        except CoreUnavailable:
            continue

    return {
        "total_bytes_used": int(link_tot[0]),
        "link_count": int(link_tot[1]),
        "subscription_bytes_used": int(sub_tot[0]),
        "subscription_count": int(sub_tot[1]),
        "active_connections": live_connections,
        "running_instances": len(running),
        "live_instances_reachable": live_ok,
        "data_sources": {"links_table": True, "live_cores": live_ok > 0 or not running},
    }


@router.get("/by-instance")
async def traffic_by_instance(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Per-instance totals from the links table + live core hourly buckets."""
    rows = (await db.execute(
        select(
            Link.instance_id,
            func.coalesce(func.sum(Link.used_bytes), 0),
            func.count(Link.id),
        ).group_by(Link.instance_id)
    )).all()
    by_instance = {r[0]: {"used_bytes": int(r[1]), "link_count": int(r[2])} for r in rows}

    out = []
    for inst in (await db.execute(select(Instance).order_by(Instance.created_at))).scalars():
        agg = by_instance.get(inst.id, {"used_bytes": 0, "link_count": 0})
        hourly: dict = {}
        if inst.status == InstanceStatus.RUNNING and inst.core_port:
            try:
                stats = await CoreClient(inst.core_port, inst.core_api_token).stats()
                hourly = dict(stats.get("hourly") or {})
                agg["active_connections"] = int(stats.get("active_connections") or 0)
                agg["total_bytes_runtime"] = int(stats.get("total_bytes") or 0)
            except CoreUnavailable:
                agg["core_unreachable"] = True
        out.append({
            "instance_id": inst.id,
            "name": inst.name,
            "status": inst.status.value,
            "used_bytes": agg["used_bytes"],
            "link_count": agg["link_count"],
            "hourly": hourly,
            **({"active_connections": agg["active_connections"]} if "active_connections" in agg else {}),
        })
    return {"instances": out}


@router.get("/by-subscription")
async def traffic_by_subscription(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(get_current_user),
):
    """Per-subscription usage from the synced counters."""
    subs = (await db.execute(
        select(Subscription).order_by(Subscription.traffic_used_bytes.desc()).limit(limit)
    )).scalars().all()
    return {
        "subscriptions": [
            {
                "id": s.id,
                "name": s.name,
                "user_id": s.user_id,
                "used_bytes": s.traffic_used_bytes,
                "limit_bytes": s.traffic_limit_bytes,
                "usage_percent": s.traffic_usage_percent,
                "effective_status": s.effective_status,
            }
            for s in subs
        ]
    }


@router.get("/top-links")
async def top_links(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    _: User = Depends(get_current_user),
):
    """Heaviest links by synced usage."""
    links = (await db.execute(
        select(Link).order_by(Link.used_bytes.desc()).limit(limit)
    )).scalars().all()
    return {
        "links": [
            {
                "id": l.id,
                "uuid": l.uuid,
                "label": l.label,
                "protocol": l.protocol,
                "instance_id": l.instance_id,
                "used_bytes": l.used_bytes,
                "limit_bytes": l.limit_bytes,
                "subscription_id": l.subscription_id,
            }
            for l in links
        ]
    }
