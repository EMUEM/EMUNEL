"""EMUNEL API — Subscription model."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String,
    Boolean,
    DateTime,
    Integer,
    Float,
    ForeignKey,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class Subscription(Base):
    """User subscription model.

    Manages traffic quotas, expiry, auto-renewal, and device limits.
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Traffic
    traffic_limit_gb: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )  # None = unlimited
    traffic_used_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # Duration
    days_limit: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # None = no expiry
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Auto-management
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_disable: Mapped[bool] = mapped_column(Boolean, default=True)

    # Device limit
    device_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active_devices: Mapped[int] = mapped_column(Integer, default=0)

    # State
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reset_day: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # Monthly reset day (1-28)
    notify_expiry: Mapped[bool] = mapped_column(Boolean, default=True)

    # Subscription link token
    link_token: Mapped[str] = mapped_column(
        String(64), unique=True, default=lambda: uuid.uuid4().hex, index=True
    )

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    user = relationship("User", back_populates="subscriptions")

    @property
    def traffic_limit_bytes(self) -> Optional[int]:
        """Traffic limit in bytes."""
        if self.traffic_limit_gb is None:
            return None
        return int(self.traffic_limit_gb * 1024 * 1024 * 1024)

    @property
    def traffic_remaining_bytes(self) -> Optional[int]:
        """Remaining traffic in bytes."""
        limit = self.traffic_limit_bytes
        if limit is None:
            return None
        return max(0, limit - self.traffic_used_bytes)

    @property
    def traffic_usage_percent(self) -> Optional[float]:
        """Traffic usage percentage."""
        limit = self.traffic_limit_bytes
        if limit is None or limit == 0:
            return None
        return min(100.0, (self.traffic_used_bytes / limit) * 100)

    @property
    def is_expired(self) -> bool:
        """Check if subscription has expired."""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    @property
    def is_quota_exceeded(self) -> bool:
        """Check if traffic quota is exceeded."""
        remaining = self.traffic_remaining_bytes
        if remaining is None:
            return False
        return remaining <= 0

    def __repr__(self) -> str:
        return f"<Subscription {self.name} (user={self.user_id[:8]}...)>"
