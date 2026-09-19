"""Operational log API placeholder with an honest empty state."""
from fastapi import APIRouter, Depends
from ...models.user import User
from ...services.auth import require_admin

router = APIRouter()

@router.get("")
async def list_logs(_: User = Depends(require_admin)):
    return {"items": [], "total": 0, "degraded": True, "message": "Structured log storage is not configured."}
