"""EMUNEL API — Instance and Link models.

An Instance is an isolated EMUNEL Core runtime: its own process, port,
state file, management token, protocols, quota and traffic accounting.
Changes to Instance A never affect Instance B.

A Link is a credential inside an instance (uuid + protocol + policy) that
the Core enforces fail-closed: unknown, disabled, expired or over-quota
links cannot relay. The DB row mirrors the Core's runtime state and is
synced by the LinkSync service.
"""

import enum
import secrets
import uuid as uuid_mod
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    String,
    Boolean,
    DateTime,
    Integer,
    Float,
    BigInteger,
    JSON,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class InstanceStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"


def _new_core_token() -> str:
    return secrets.token_urlsafe(32)


def _new_link_uuid() -> str:
    """Canonical UUID form (the Core's wire format for VLESS/xHTTP paths)."""
    return str(uuid_mod.uuid4())


class Instance(Base):
    """Isolated proxy runtime managed by the InstanceManager."""

    __tablename__ = "instances"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid_mod.uuid4())
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(64), default="local", nullable=False)

    status: Mapped[InstanceStatus] = mapped_column(
        Enum(InstanceStatus), default=InstanceStatus.STOPPED, nullable=False, index=True
    )

    # Protocol matrix — validated against the Core's supported set at create time.
    protocols: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )  # {"enabled": ["vless-ws", "trojan-ws", ...], "default": "vless-ws"}

    # Resource limits applied to the Core subprocess.
    cpu_limit: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, default=256, nullable=False)
    max_processes: Mapped[int] = mapped_column(Integer, default=128, nullable=False)

    # Runtime binding (assigned by InstanceManager at launch).
    core_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    core_pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Secret: never returned by any API response.
    core_api_token: Mapped[str] = mapped_column(
        String(64), default=_new_core_token, nullable=False
    )
    state_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Public host rendered into share links (set by admin / reverse proxy setup).
    public_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_active_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    links = relationship(
        "Link", back_populates="instance", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Instance {self.name} ({self.status.value})>"


class Link(Base):
    """A credential enforced by an instance's Core (fail-closed)."""

    __tablename__ = "links"
    __table_args__ = (UniqueConstraint("instance_id", "uuid", name="uq_link_instance_uuid"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid_mod.uuid4())
    )
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The wire credential (VLESS UUID / Trojan password / SS key material).
    uuid: Mapped[str] = mapped_column(String(36), default=_new_link_uuid, nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="Link", nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)

    # Policy — enforced by the Core, mirrored here.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    limit_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # 0 = unlimited
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    # Shadowsocks secrets — never returned by API responses.
    ss_cipher: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    ss_password: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Optional subscription binding.
    subscription_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # selectin: eagerly batch-loaded so async route code can access the
    # relationship without triggering implicit sync IO (MissingGreenlet).
    instance = relationship("Instance", back_populates="links", lazy="selectin")
    subscription = relationship("Subscription", back_populates="links", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Link {self.uuid[:8]} ({self.protocol})>"
