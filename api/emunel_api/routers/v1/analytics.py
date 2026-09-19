"""EMUNEL API — Analytics router."""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.node import Node, NodeStatus
from ...models.subscription import Subscription
from ...models.user import User
from ...models.audit import AuditLog
from ...services.auth import require_admin

router = APIRouter()


@router.get("/overview")
async def analytics_overview(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Get platform overview analytics."""
    # User counts
    total_users = (await db.execute(
        select(func.count()).select_from(User)
    )).scalar() or 0

    active_users = (await db.execute(
        select(func.count()).select_from(User).where(User.is_active == True)
    )).scalar() or 0

    # Node counts
    total_nodes = (await db.execute(
        select(func.count()).select_from(Node)
    )).scalar() or 0

    online_nodes = (await db.execute(
        select(func.count()).select_from(Node).where(Node.status == NodeStatus.ONLINE)
    )).scalar() or 0

    # Subscription counts
    total_subs = (await db.execute(
        select(func.count()).select_from(Subscription)
    )).scalar() or 0

    active_subs = (await db.execute(
        select(func.count()).select_from(Subscription).where(Subscription.is_active == True)
    )).scalar() or 0

    # Total traffic
    traffic = (await db.execute(
        select(
            func.coalesce(func.sum(Node.total_bytes_up), 0),
            func.coalesce(func.sum(Node.total_bytes_down), 0),
        )
    )).one()

    # Total connections
    total_connections = (await db.execute(
        select(func.coalesce(func.sum(Node.active_connections), 0))
    )).scalar() or 0

    return {
        "users": {"total": total_users, "active": active_users},
        "nodes": {"total": total_nodes, "online": online_nodes},
        "subscriptions": {"total": total_subs, "active": active_subs},
        "traffic": {
            "upload_bytes": traffic[0],
            "download_bytes": traffic[1],
            "total_bytes": traffic[0] + traffic[1],
        },
        "connections": total_connections,
    }


@router.get("/recent-activity")
async def recent_activity(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Get recent audit log activity."""
    result = await db.execute(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    return [
        {
            "id": log.id,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "detail": log.detail,
            "user_id": log.user_id,
            "ip_address": log.ip_address,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]
