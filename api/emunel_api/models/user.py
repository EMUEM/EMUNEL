"""EMUNEL API — User model."""

import enum
import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    String,
    Boolean,
    DateTime,
    Enum,
    Integer,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"
    VIEWER = "viewer"


class User(Base):
    """User account model."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, nullable=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.USER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    subscriptions = relationship("Subscription", back_populates="user", lazy="selectin")
    audit_logs = relationship("AuditLog", back_populates="user", lazy="selectin")

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role.value})>"
