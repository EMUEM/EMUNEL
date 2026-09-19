"""EMUNEL API — Subscriptions router (integrated with instances + Core links).

A subscription is the user-facing policy container. Its traffic quota,
expiry and revocation state are pushed into the instance's Core link
registry — the Core enforces them fail-closed on every connection.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.instance import Instance, InstanceStatus, Link
from ...models.subscription import Subscription
from ...models.user import User
from ...services.auth import get_current_user, require_admin
from ...services import link_sync

router = APIRouter()

DURATION_PRESETS_DAYS = (1, 7, 30, 60, 90)


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
    effective_status: str = "active"
    reset_day: Optional[int] = None
    link_token: str
    instance_ids: List[str] = []
    link_count: int = 0
    created_at: Optional[str] = None


class SubCreate(BaseModel):
    user_id: str
    name: str = Field(..., min_length=1, max_length=128)
    traffic_limit_gb: Optional[float] = Field(None, ge=0)  # None = unlimited
    days_limit: Optional[int] = Field(None, ge=1)  # None = unlimited
    auto_renew: bool = False
    auto_disable: bool = True
    device_limit: Optional[int] = Field(None, ge=1, le=64)
    reset_day: Optional[int] = Field(None, ge=1, le=28)
    notify_expiry: bool = True
    # link provisioning
    instance_id: Optional[str] = None
    protocol: Optional[str] = None
    link_label: Optional[str] = None
    links_per_instance: int = Field(default=1, ge=1, le=8)


class SubUpdate(BaseModel):
    name: Optional[str] = None
    traffic_limit_gb: Optional[float] = Field(None, ge=0)
    days_limit: Optional[int] = Field(None, ge=1)
    auto_renew: Optional[bool] = None
    auto_disable: Optional[bool] = None
    device_limit: Optional[int] = Field(None, ge=1, le=64)
    is_active: Optional[bool] = None
    reset_day: Optional[int] = Field(None, ge=1, le=28)
    notify_expiry: Optional[bool] = None


class ExpiryExtend(BaseModel):
    days: int = Field(..., ge=1, le=3650)


def _sub_to_out(sub: Subscription) -> SubOut:
    links = sub.links or []
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
        effective_status=sub.effective_status,
        reset_day=sub.reset_day,
        link_token=sub.link_token,
        instance_ids=sorted({l.instance_id for l in links}),
        link_count=len(links),
        created_at=sub.created_at.isoformat() if sub.created_at else None,
    )


async def _provision_links(db: AsyncSession, sub: Subscription, body: SubCreate) -> None:
    """Create link rows for the subscription on its target instance and
    push them into the running Core."""
    if not body.instance_id:
        return
    inst = (await db.execute(select(Instance).where(Instance.id == body.instance_id))).scalar_one_or_none()
    if inst is None:
        raise HTTPException(404, detail="target instance not found")

    enabled = (inst.protocols or {}).get("enabled") or ["vless-ws"]
    protocols = []
    if body.protocol:
        if body.protocol not in enabled:
            raise HTTPException(400, detail=f"protocol {body.protocol} not enabled on instance {inst.name}")
        protocols = [body.protocol]
    else:
        protocols = [enabled[0]]

    import secrets as _secrets

    for i in range(body.links_per_instance):
        for proto in protocols:
            link = Link(
                instance_id=inst.id,
                label=(body.link_label or sub.name)[:80],
                protocol=proto,
                active=True,
                limit_bytes=0,  # quota comes from the subscription policy
                expires_at=None,
                subscription_id=sub.id,
                ss_cipher="chacha20-ietf-poly1305" if proto == "shadowsocks" else None,
                ss_password=_secrets.token_urlsafe(16) if proto == "shadowsocks" else None,
            )
            db.add(link)
    await db.commit()
    await db.refresh(sub)

    if inst.status == InstanceStatus.RUNNING:
        for link in sub.links:
            await link_sync.push_link(db, link)


@router.get("")
async def list_subscriptions(
    user_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List subscriptions (paginated; users see only their own)."""
    query = select(Subscription)
    if current_user.role.value != "admin":
        query = query.where(Subscription.user_id == current_user.id)
    elif user_id:
        query = query.where(Subscription.user_id == user_id)
    if is_active is not None:
        query = query.where(Subscription.is_active == is_active)

    total = len((await db.execute(query)).scalars().all())
    rows = (await db.execute(query.order_by(Subscription.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    subs = [_sub_to_out(s) for s in rows]
    if status:
        subs = [s for s in subs if s.effective_status == status]
    return {"subscriptions": subs, "total": total, "limit": limit, "offset": offset}


@router.post("", response_model=SubOut, status_code=201)
async def create_subscription(
    data: SubCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    user = (await db.execute(select(User).where(User.id == data.user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, detail="User not found")

    expires_at = None
    if data.days_limit:
        expires_at = datetime.now(timezone.utc) + timedelta(days=data.days_limit)

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

    await _provision_links(db, sub, data)
    await db.refresh(sub)
    return _sub_to_out(sub)


@router.get("/{sub_id}", response_model=SubOut)
async def get_subscription(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")
    if current_user.role.value != "admin" and sub.user_id != current_user.id:
        raise HTTPException(403, detail="Access denied")
    return _sub_to_out(sub)


@router.patch("/{sub_id}", response_model=SubOut)
async def update_subscription(
    sub_id: str,
    data: SubUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")

    updates = data.model_dump(exclude_unset=True)
    if "traffic_limit_gb" in updates:
        sub.traffic_limit_gb = updates["traffic_limit_gb"]
    if "days_limit" in updates and updates["days_limit"] is not None:
        sub.days_limit = updates["days_limit"]
        sub.expires_at = datetime.now(timezone.utc) + timedelta(days=updates["days_limit"])
    for field in ("name", "auto_renew", "auto_disable", "device_limit", "reset_day", "notify_expiry"):
        if field in updates:
            setattr(sub, field, updates[field])
    if "is_active" in updates:
        sub.is_active = updates["is_active"]

    await db.commit()
    await db.refresh(sub)

    # propagate policy onto every bound link (Core-side enforcement)
    await link_sync.apply_subscription_policy(db, sub)
    await db.commit()
    return _sub_to_out(sub)


@router.delete("/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")
    # links survive (FK SET NULL) but are revoked on the Core first
    for link in sub.links:
        await link_sync.revoke_link(db, link)
        link.active = False
        link.subscription_id = None
    await db.commit()
    await db.delete(sub)
    await db.commit()


@router.post("/{sub_id}/reset-traffic", response_model=SubOut)
async def reset_traffic(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")
    for link in sub.links:
        link.used_bytes = 0
        await link_sync.push_link(db, link)
        if link.instance and link.instance.status == InstanceStatus.RUNNING:
            from ...services.core_client import CoreClient
            try:
                CoreClient(link.instance.core_port, link.instance.core_api_token).update_link(
                    link.uuid, {"reset_usage": True}
                )
            except Exception:
                pass
    sub.traffic_used_bytes = 0
    sub.last_reset_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(sub)
    return _sub_to_out(sub)


@router.post("/{sub_id}/extend", response_model=SubOut)
async def extend_expiry(
    sub_id: str,
    body: ExpiryExtend,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")
    base = sub.expires_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    sub.expires_at = base + timedelta(days=body.days)
    sub.days_limit = (sub.days_limit or 0) + body.days if sub.days_limit else None
    await db.commit()
    await db.refresh(sub)
    await link_sync.apply_subscription_policy(db, sub)
    await db.commit()
    return _sub_to_out(sub)


@router.post("/{sub_id}/revoke", response_model=SubOut)
async def revoke_subscription(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")
    sub.is_active = False
    for link in sub.links:
        link.active = False
    await db.commit()
    for link in sub.links:
        await link_sync.revoke_link(db, link)
    await db.refresh(sub)
    return _sub_to_out(sub)


@router.post("/{sub_id}/renew", response_model=SubOut)
async def renew_subscription(
    sub_id: str,
    body: ExpiryExtend,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Renew: extend expiry from now and re-activate if not quota-blocked."""
    sub = (await db.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, detail="Subscription not found")
    sub.expires_at = datetime.now(timezone.utc) + timedelta(days=body.days)
    sub.days_limit = body.days
    if not sub.is_quota_exceeded:
        sub.is_active = True
    await db.commit()
    await db.refresh(sub)
    await link_sync.apply_subscription_policy(db, sub)
    await db.commit()
    return _sub_to_out(sub)
