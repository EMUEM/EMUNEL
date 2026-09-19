"""EMUNEL API — Node model."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String,
    Boolean,
    DateTime,
    Enum,
    Integer,
    Float,
    JSON,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class NodeStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    MAINTENANCE = "maintenance"


class Node(Base):
    """Proxy node model."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=443)
    protocol: Mapped[str] = mapped_column(
        String(32), default="vless", nullable=False
    )  # vless, trojan, shadowsocks
    status: Mapped[NodeStatus] = mapped_column(
        Enum(NodeStatus), default=NodeStatus.OFFLINE
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Performance
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    uptime_percent: Mapped[float] = mapped_column(Float, default=0.0)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Traffic
    total_bytes_up: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes_down: Mapped[int] = mapped_column(Integer, default=0)
    active_connections: Mapped[int] = mapped_column(Integer, default=0)
    max_connections: Mapped[int] = mapped_column(Integer, default=1000)

    # Configuration
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    worker_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Metadata
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)  # ISO 3166-1
    region: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tags: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Node {self.name} ({self.status.value})>"
