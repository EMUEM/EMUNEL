"""EMUNEL API — Health router with independent component states."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...services import health as health_svc

router = APIRouter()


@router.get("/live")
async def liveness():
    """Process liveness — answering this proves the event loop is alive."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(db: AsyncSession = Depends(get_db)):
    """Readiness — the API can do useful work (DB reachable)."""
    db_health = await health_svc.database_health(db)
    ready = db_health["status"] == "ok"
    return {"ready": ready, "database": db_health}


@router.get("/full")
async def full_health(db: AsyncSession = Depends(get_db)):
    """All components, independently reported (never a single global fail)."""
    return await health_svc.full_health(db)


@router.get("/database")
async def database_health(db: AsyncSession = Depends(get_db)):
    return await health_svc.database_health(db)


@router.get("/instances")
async def instances_health(db: AsyncSession = Depends(get_db)):
    return await health_svc.instances_health(db)


@router.get("/sync")
async def sync_health():
    return health_svc.sync_health()


@router.get("/manager")
async def manager_health():
    return health_svc.manager_health()
