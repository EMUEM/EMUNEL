"""EMUNEL API — Database setup with SQLAlchemy async.

Supports PostgreSQL (asyncpg) for production and SQLite (aiosqlite) for development.
"""

import logging
import os
from pathlib import Path
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
else:
    # Postgres: fail fast per attempt (default connect timeout is 60s,
    # which would stall startup on unreachable hosts)
    _engine_kwargs["connect_args"] = {"timeout": 10, "command_timeout": 10}
    # the default DSN lives under EMUNEL_DATA_ROOT (a volume in deployments)
    # — make sure the directory exists before the engine opens the file
    _sqlite_path = settings.database_url.split(":///", 1)[-1]
    if _sqlite_path and _sqlite_path != ":memory:":
        Path(_sqlite_path).parent.mkdir(parents=True, exist_ok=True)

engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _init_attempts() -> int:
    """Retry budget from env (Railway: Postgres may still be provisioning)."""
    try:
        return max(1, int(os.environ.get("EMUNEL_DB_INIT_ATTEMPTS", "30")))
    except ValueError:
        return 30


async def init_db(max_attempts: int | None = None, delay: float = 3.0) -> None:
    """Initialize database tables — tolerates the platform start-up race
    where PostgreSQL is still provisioning (reference pattern: retry
    connection errors for ~90s; never crash the container).

    Auth/config errors (bad DSN) surface immediately with a clear message;
    connection errors retry. After exhausting attempts the exception
    propagates to the caller, which keeps serving /health and retries in
    the background instead of exiting.
    """
    import asyncio

    from .models import user, node, subscription, audit, instance  # noqa: F401

    if max_attempts is None:
        max_attempts = _init_attempts()
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info(
                "Database initialized: %s",
                settings.database_url.split("@")[-1] if "@" in settings.database_url
                else settings.database_url,
            )
            return
        except Exception as exc:  # noqa: BLE001 — classify below
            last = exc
            msg = str(exc).lower()
            fatal = any(
                token in msg
                for token in ("password authentication failed", "does not exist", "permission denied")
            )
            if fatal:
                logger.error("Database rejected the configuration: %s", exc)
                raise
            if attempt < max_attempts:
                logger.warning(
                    "database not reachable yet (attempt %d/%d): %s — retrying in %.0fs",
                    attempt, max_attempts, type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
    raise RuntimeError(f"database unreachable after {max_attempts} attempts: {last}")


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
