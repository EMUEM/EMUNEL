"""EMUNEL API — Online connections router (live data from running Cores)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.instance import Instance, InstanceStatus
from ...models.user import User
from ...services.auth import get_current_user
from ...services.core_client import CoreClient, CoreUnavailable

router = APIRouter()


@router.get("")
async def online_connections(
    instance_id: str = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Live connections grouped by client IP, merged across instances.

    Real data from the Cores' connection trackers. Empty list when nothing
    is running or no client is connected — never simulated.
    """
    query = select(Instance).where(Instance.status == InstanceStatus.RUNNING)
    if instance_id:
        query = query.where(Instance.id == instance_id)

    groups = []
    total = 0
    reachable = 0
    for inst in (await db.execute(query)).scalars():
        try:
            conns = await CoreClient(inst.core_port, inst.core_api_token).connections()
            reachable += 1
            raw = int(conns.get("raw_count") or 0)
            total += raw
            for g in conns.get("connections") or []:
                g = dict(g)
                g["instance_id"] = inst.id
                g["instance_name"] = inst.name
                groups.append(g)
        except CoreUnavailable:
            continue

    groups.sort(key=lambda x: x.get("last_connected_at") or "", reverse=True)
    return {
        "connections": groups,
        "raw_count": total,
        "grouped_count": len(groups),
        "instances_running": reachable,
        "degraded_instances": [],  # filled below
    }


@router.get("/summary")
async def connections_summary(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Compact live summary for dashboard badges."""
    running = (await db.execute(
        select(Instance).where(Instance.status == InstanceStatus.RUNNING)
    )).scalars().all()

    per_instance = []
    total = 0
    for inst in running:
        entry = {"instance_id": inst.id, "name": inst.name, "connections": 0, "reachable": False}
        try:
            conns = await CoreClient(inst.core_port, inst.core_api_token).connections()
            entry["connections"] = int(conns.get("raw_count") or 0)
            entry["reachable"] = True
            total += entry["connections"]
        except CoreUnavailable:
            pass
        per_instance.append(entry)

    return {
        "total_connections": total,
        "instances": per_instance,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
