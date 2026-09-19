"""EMUNEL API — Nodes router."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.node import Node, NodeStatus
from ...services.auth import get_current_user, require_admin
from ...models.user import User

router = APIRouter()


class NodeOut(BaseModel):
    id: str
    name: str
    address: str
    port: int
    protocol: str
    status: str
    is_enabled: bool
    latency_ms: Optional[float] = None
    uptime_percent: float = 0.0
    last_seen: Optional[str] = None
    active_connections: int = 0
    max_connections: int = 1000
    country: Optional[str] = None
    region: Optional[str] = None
    total_bytes_up: int = 0
    total_bytes_down: int = 0
    created_at: Optional[str] = None


class NodeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str
    port: int = Field(default=443, ge=1, le=65535)
    protocol: str = Field(default="vless")
    max_connections: int = Field(default=1000, ge=1)
    country: Optional[str] = None
    region: Optional[str] = None
    config: Optional[dict] = None


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None
    is_enabled: Optional[bool] = None
    max_connections: Optional[int] = None
    country: Optional[str] = None
    region: Optional[str] = None
    config: Optional[dict] = None


@router.get("", response_model=List[NodeOut])
async def list_nodes(
    status_filter: Optional[str] = Query(None, alias="status"),
    protocol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all nodes."""
    query = select(Node)
    if status_filter:
        query = query.where(Node.status == NodeStatus(status_filter))
    if protocol:
        query = query.where(Node.protocol == protocol)

    result = await db.execute(query)
    nodes = result.scalars().all()

    return [
        NodeOut(
            id=n.id,
            name=n.name,
            address=n.address,
            port=n.port,
            protocol=n.protocol,
            status=n.status.value,
            is_enabled=n.is_enabled,
            latency_ms=n.latency_ms,
            uptime_percent=n.uptime_percent,
            last_seen=n.last_seen.isoformat() if n.last_seen else None,
            active_connections=n.active_connections,
            max_connections=n.max_connections,
            country=n.country,
            region=n.region,
            total_bytes_up=n.total_bytes_up,
            total_bytes_down=n.total_bytes_down,
            created_at=n.created_at.isoformat() if n.created_at else None,
        )
        for n in nodes
    ]


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(
    data: NodeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a new node."""
    node = Node(
        name=data.name,
        address=data.address,
        port=data.port,
        protocol=data.protocol,
        max_connections=data.max_connections,
        country=data.country,
        region=data.region,
        config=data.config,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)

    return NodeOut(
        id=node.id,
        name=node.name,
        address=node.address,
        port=node.port,
        protocol=node.protocol,
        status=node.status.value,
        is_enabled=node.is_enabled,
        latency_ms=node.latency_ms,
        uptime_percent=node.uptime_percent,
        last_seen=None,
        active_connections=node.active_connections,
        max_connections=node.max_connections,
        country=node.country,
        region=node.region,
        total_bytes_up=node.total_bytes_up,
        total_bytes_down=node.total_bytes_down,
        created_at=node.created_at.isoformat() if node.created_at else None,
    )


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific node."""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    return NodeOut(
        id=node.id,
        name=node.name,
        address=node.address,
        port=node.port,
        protocol=node.protocol,
        status=node.status.value,
        is_enabled=node.is_enabled,
        latency_ms=node.latency_ms,
        uptime_percent=node.uptime_percent,
        last_seen=node.last_seen.isoformat() if node.last_seen else None,
        active_connections=node.active_connections,
        max_connections=node.max_connections,
        country=node.country,
        region=node.region,
        total_bytes_up=node.total_bytes_up,
        total_bytes_down=node.total_bytes_down,
        created_at=node.created_at.isoformat() if node.created_at else None,
    )


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: str,
    data: NodeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update a node."""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(node, field, value)

    await db.commit()
    await db.refresh(node)

    return NodeOut(
        id=node.id,
        name=node.name,
        address=node.address,
        port=node.port,
        protocol=node.protocol,
        status=node.status.value,
        is_enabled=node.is_enabled,
        latency_ms=node.latency_ms,
        uptime_percent=node.uptime_percent,
        last_seen=node.last_seen.isoformat() if node.last_seen else None,
        active_connections=node.active_connections,
        max_connections=node.max_connections,
        country=node.country,
        region=node.region,
        total_bytes_up=node.total_bytes_up,
        total_bytes_down=node.total_bytes_down,
        created_at=node.created_at.isoformat() if node.created_at else None,
    )


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a node."""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    await db.delete(node)
    await db.commit()
