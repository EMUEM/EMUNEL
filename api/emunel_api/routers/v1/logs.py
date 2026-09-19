"""EMUNEL API — Logs router: audit trail (DB) + live Core logs (per instance)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.audit import AuditLog
from ...models.instance import Instance, InstanceStatus
from ...models.user import User
from ...services.auth import get_current_user, require_admin
from ...services.core_client import CoreClient, CoreUnavailable

router = APIRouter()


@router.get("/audit")
async def audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Administrative audit trail from the database."""
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        query = query.where(AuditLog.action == action)
    rows = (await db.execute(query.offset(offset).limit(limit))).scalars().all()
    return {
        "logs": [
            {
                "id": e.id,
                "action": e.action,
                "resource_type": e.resource_type,
                "resource_id": e.resource_id,
                "detail": e.detail,
                "user_id": e.user_id,
                "ip_address": e.ip_address,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]
    }


@router.get("/instance/{instance_id}")
async def instance_core_logs(
    instance_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Live runtime logs from an instance's Core ring buffer (redacted)."""
    inst = (await db.execute(select(Instance).where(Instance.id == instance_id))).scalar_one_or_none()
    if inst is None:
        raise HTTPException(404, detail="instance not found")
    if inst.status != InstanceStatus.RUNNING or not inst.core_port:
        raise HTTPException(409, detail="instance is not running (no live logs)")
    try:
        logs = await CoreClient(inst.core_port, inst.core_api_token).logs(limit)
        return {"logs": logs, "instance_id": inst.id, "instance_name": inst.name}
    except CoreUnavailable as exc:
        raise HTTPException(503, detail=str(exc))
