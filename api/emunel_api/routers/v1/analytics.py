"""EMUNEL API — Analytics router (aggregated from real data only)."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.instance import Instance, InstanceStatus, Link
from ...models.subscription import Subscription
from ...models.user import User, UserRole
from ...models.audit import AuditLog
from ...services.auth import require_admin

router = APIRouter()


@router.get("/overview")
async def analytics_overview(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Platform overview from persisted state."""
    users_total = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
    users_active = (await db.execute(
        select(func.count()).select_from(User).where(User.is_active == True)  # noqa: E712
    )).scalar() or 0

    instances_total = (await db.execute(select(func.count()).select_from(Instance))).scalar() or 0
    instances_running = (await db.execute(
        select(func.count()).select_from(Instance).where(Instance.status == InstanceStatus.RUNNING)
    )).scalar() or 0

    links_total = (await db.execute(select(func.count()).select_from(Link))).scalar() or 0
    links_active = (await db.execute(
        select(func.count()).select_from(Link).where(Link.active == True)  # noqa: E712
    )).scalar() or 0

    subs_total = (await db.execute(select(func.count()).select_from(Subscription))).scalar() or 0

    traffic = (await db.execute(
        select(func.coalesce(func.sum(Link.used_bytes), 0))
    )).scalar() or 0

    events_24h = (await db.execute(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.created_at >= datetime.now(timezone.utc) - timedelta(hours=24)
        )
    )).scalar() or 0

    return {
        "users": {"total": users_total, "active": users_active},
        "instances": {"total": instances_total, "running": instances_running},
        "links": {"total": links_total, "active": links_active},
        "subscriptions": {"total": subs_total},
        "traffic_total_bytes": int(traffic),
        "audit_events_24h": int(events_24h),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/protocol-distribution")
async def protocol_distribution(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Traffic split per protocol (from synced link counters)."""
    rows = (await db.execute(
        select(Link.protocol, func.coalesce(func.sum(Link.used_bytes), 0), func.count(Link.id))
        .group_by(Link.protocol)
    )).all()
    return {
        "distribution": [
            {"protocol": r[0], "used_bytes": int(r[1]), "link_count": int(r[2])} for r in rows
        ]
    }


@router.get("/subscription-status")
async def subscription_status_distribution(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Subscription lifecycle distribution (active/expired/quota_exceeded/disabled)."""
    subs = (await db.execute(select(Subscription))).scalars().all()
    dist: dict = {}
    for s in subs:
        st = s.effective_status
        dist[st] = dist.get(st, 0) + 1
    return {"distribution": [{"status": k, "count": v} for k, v in sorted(dist.items())]}


@router.get("/hourly")
async def hourly_traffic(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Hourly traffic buckets merged across all running instances (live)."""
    from ...services.core_client import CoreClient, CoreUnavailable

    merged: dict = {}
    reachable = 0
    for inst in (await db.execute(
        select(Instance).where(Instance.status == InstanceStatus.RUNNING)
    )).scalars():
        try:
            stats = await CoreClient(inst.core_port, inst.core_api_token).stats()
            reachable += 1
            for hour, value in (stats.get("hourly") or {}).items():
                merged[hour] = merged.get(hour, 0) + int(value)
        except CoreUnavailable:
            continue
    return {
        "hourly": dict(sorted(merged.items())),
        "instances_reachable": reachable,
    }


@router.get("/recent-events")
async def recent_events(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_admin),
):
    """Recent audit events (real administrative actions)."""
    events = (await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )).scalars().all()
    return {
        "events": [
            {
                "id": e.id,
                "action": e.action,
                "resource_type": e.resource_type,
                "resource_id": e.resource_id,
                "detail": e.detail,
                "user_id": e.user_id,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]
    }
