"""Safe, non-secret runtime settings endpoint."""
from fastapi import APIRouter, Depends
from ...config import settings
from ...models.user import User
from ...services.auth import require_admin

router = APIRouter()

@router.get("")
async def get_settings(_: User = Depends(require_admin)):
    return {
        "app_name": settings.app_name,
        "debug": settings.debug,
        "database": settings.database_url.split(":", 1)[0],
        "metrics_enabled": settings.metrics_enabled,
        "rate_limit_per_minute": settings.rate_limit_per_minute,
    }
