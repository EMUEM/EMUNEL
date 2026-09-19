"""EMUNEL API — Database setup with SQLAlchemy async.

Supports PostgreSQL (asyncpg) for production and SQLite (aiosqlite) for development.
"""

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger("emunel.api.database")


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass


# Engine configuration
_engine_kwargs = {
    "echo": settings.debug,
    "pool_pre_ping": True,
}

# SQLite needs special connect_args
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Initialize database tables."""
    # Import all models so they are registered with Base.metadata
    from .models import user, node, subscription, audit, instance  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialized: %s", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency: yield a database session."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
