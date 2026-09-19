"""EMUNEL API — Traffic monitoring router."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.node import Node
from ...models.subscription import Subscription
from ...models.user import User
from ...services.auth import get_current_user, require_admin

router = APIRouter()


class TrafficSummary(BaseModel):
    total_upload_bytes: int = 0
    total_download_bytes: int = 0
    total_traffic_bytes: int = 0
    active_subscriptions: int = 0
    total_subscriptions: int = 0


class NodeTraffic(BaseModel):
    node_id: str
    name: str
    upload_bytes: int
    download_bytes: int
    active_connections: int


@router.get("/summary", response_model=TrafficSummary)
async def traffic_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Get overall traffic summary."""
    # Node traffic
    node_result = await db.execute(
        select(
            func.coalesce(func.sum(Node.total_bytes_up), 0),
            func.coalesce(func.sum(Node.total_bytes_down), 0),
        )
    )
    row = node_result.one()
    total_up = row[0]
    total_down = row[1]

    # Subscription counts
    total_subs = (await db.execute(
        select(func.count()).select_from(Subscription)
    )).scalar() or 0

    active_subs = (await db.execute(
        select(func.count()).select_from(Subscription).where(Subscription.is_active == True)
    )).scalar() or 0

    return TrafficSummary(
        total_upload_bytes=total_up,
        total_download_bytes=total_down,
        total_traffic_bytes=total_up + total_down,
        active_subscriptions=active_subs,
        total_subscriptions=total_subs,
    )


@router.get("/by-node")
async def traffic_by_node(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Get traffic breakdown by node."""
    result = await db.execute(select(Node))
    nodes = result.scalars().all()

    return [
        NodeTraffic(
            node_id=n.id,
            name=n.name,
            upload_bytes=n.total_bytes_up,
            download_bytes=n.total_bytes_down,
            active_connections=n.active_connections,
        )
        for n in nodes
    ]


@router.get("/by-user")
async def traffic_by_user(
    user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get traffic breakdown by user subscriptions."""
    query = select(Subscription)

    if current_user.role.value != "admin":
        query = query.where(Subscription.user_id == current_user.id)
    elif user_id:
        query = query.where(Subscription.user_id == user_id)

    result = await db.execute(query)
    subs = result.scalars().all()

    return [
        {
            "subscription_id": s.id,
            "name": s.name,
            "user_id": s.user_id,
            "traffic_used_bytes": s.traffic_used_bytes,
            "traffic_limit_gb": s.traffic_limit_gb,
            "usage_percent": s.traffic_usage_percent,
        }
        for s in subs
    ]
