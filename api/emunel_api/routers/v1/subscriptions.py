"""EMUNEL API — Subscriptions router."""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.subscription import Subscription
from ...models.user import User
from ...services.auth import get_current_user, require_admin

router = APIRouter()


class SubOut(BaseModel):
    id: str
    user_id: str
    name: str
    traffic_limit_gb: Optional[float] = None
    traffic_used_bytes: int = 0
    traffic_usage_percent: Optional[float] = None
    days_limit: Optional[int] = None
    expires_at: Optional[str] = None
    auto_renew: bool = False
    auto_disable: bool = True
    device_limit: Optional[int] = None
    active_devices: int = 0
    is_active: bool = True
    reset_day: Optional[int] = None
    link_token: str
    created_at: Optional[str] = None


class SubCreate(BaseModel):
    user_id: str
    name: str = Field(..., min_length=1, max_length=128)
    traffic_limit_gb: Optional[float] = Field(None, ge=0)
    days_limit: Optional[int] = Field(None, ge=1)
    auto_renew: bool = False
    auto_disable: bool = True
    device_limit: Optional[int] = Field(None, ge=1)
    reset_day: Optional[int] = Field(None, ge=1, le=28)
    notify_expiry: bool = True


class SubUpdate(BaseModel):
    name: Optional[str] = None
    traffic_limit_gb: Optional[float] = None
    days_limit: Optional[int] = None
    auto_renew: Optional[bool] = None
    auto_disable: Optional[bool] = None
    device_limit: Optional[int] = None
    is_active: Optional[bool] = None
    reset_day: Optional[int] = None
    notify_expiry: Optional[bool] = None


def _sub_to_out(sub: Subscription) -> SubOut:
    return SubOut(
        id=sub.id,
        user_id=sub.user_id,
        name=sub.name,
        traffic_limit_gb=sub.traffic_limit_gb,
        traffic_used_bytes=sub.traffic_used_bytes,
        traffic_usage_percent=sub.traffic_usage_percent,
        days_limit=sub.days_limit,
        expires_at=sub.expires_at.isoformat() if sub.expires_at else None,
        auto_renew=sub.auto_renew,
        auto_disable=sub.auto_disable,
        device_limit=sub.device_limit,
        active_devices=sub.active_devices,
        is_active=sub.is_active,
        reset_day=sub.reset_day,
        link_token=sub.link_token,
        created_at=sub.created_at.isoformat() if sub.created_at else None,
    )


@router.get("", response_model=List[SubOut])
async def list_subscriptions(
    user_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List subscriptions."""
    query = select(Subscription)

    # Non-admin users can only see their own
    if current_user.role.value != "admin":
        query = query.where(Subscription.user_id == current_user.id)
    elif user_id:
        query = query.where(Subscription.user_id == user_id)

    if is_active is not None:
        query = query.where(Subscription.is_active == is_active)

    result = await db.execute(query)
    subs = result.scalars().all()
    return [_sub_to_out(s) for s in subs]


@router.post("", response_model=SubOut, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    data: SubCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a new subscription."""
    # Verify user exists
    user_result = await db.execute(select(User).where(User.id == data.user_id))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    expires_at = None
    if data.days_limit:
        expires_at = datetime.utcnow() + timedelta(days=data.days_limit)

    sub = Subscription(
        user_id=data.user_id,
        name=data.name,
        traffic_limit_gb=data.traffic_limit_gb,
        days_limit=data.days_limit,
        expires_at=expires_at,
        auto_renew=data.auto_renew,
        auto_disable=data.auto_disable,
        device_limit=data.device_limit,
        reset_day=data.reset_day,
        notify_expiry=data.notify_expiry,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    return _sub_to_out(sub)


@router.get("/{sub_id}", response_model=SubOut)
async def get_subscription(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific subscription."""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if current_user.role.value != "admin" and sub.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    return _sub_to_out(sub)


@router.patch("/{sub_id}", response_model=SubOut)
async def update_subscription(
    sub_id: str,
    data: SubUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update a subscription."""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    update_data = data.model_dump(exclude_unset=True)

    # Recalculate expiry if days_limit changed
    if "days_limit" in update_data and update_data["days_limit"] is not None:
        sub.expires_at = datetime.utcnow() + timedelta(days=update_data["days_limit"])

    for field, value in update_data.items():
        setattr(sub, field, value)

    await db.commit()
    await db.refresh(sub)

    return _sub_to_out(sub)


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a subscription."""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    await db.delete(sub)
    await db.commit()


@router.post("/{sub_id}/reset-traffic", response_model=SubOut)
async def reset_traffic(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Reset traffic counter for a subscription."""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub.traffic_used_bytes = 0
    await db.commit()
    await db.refresh(sub)

    return _sub_to_out(sub)
