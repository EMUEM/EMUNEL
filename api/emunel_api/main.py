"""EMUNEL API — FastAPI application (unified console)."""

import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func

from .config import settings
from .database import init_db, async_session
from .middleware.auth import AuthMiddleware
from .models import user as user_model, instance as instance_model, subscription as sub_model
from .models.instance import Instance, InstanceStatus, Link
from .models.subscription import Subscription
from .models.user import User, UserRole
from .services.auth import hash_password
from .services import instance_manager as im
from .services import link_sync
from .routers.v1 import (
    auth,
    users,
    nodes,
    instances,
    subscriptions,
    traffic,
    analytics,
    connections,
    network_tests,
    logs,
    settings as settings_router,
    health as health_router,
)

logger = logging.getLogger("emunel.api")

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"


async def _seed_admin() -> None:
    """Create the initial admin account if the users table is empty."""
    from .database import async_session

    async with async_session() as db:
        count = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
        if count:
            return
        admin = User(
            username=settings.admin_username,
            email=None,
            hashed_password=hash_password(settings.admin_password),
            role=UserRole.ADMIN,
            is_active=True,
            is_verified=True,
        )
        db.add(admin)
        await db.commit()
        logger.warning("seeded initial admin account %r — change this password", settings.admin_username)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Startup: validate config, open DB, recover instances, start sync."""
    settings.validate_runtime()
    await init_db()
    await _seed_admin()

    # Instance manager + restart recovery (relaunches registered Cores with
    # identical credentials; state files preserve links and counters).
    im.manager = im.InstanceManager()
    recovered = await im.manager.recover()

    # Link sync worker: pushes policy, pulls counters (batched).
    link_sync.sync_worker.start(async_session)

    logger.info("EMUNEL API up (instances recovered: %d)", recovered)
    try:
        yield
    finally:
        await link_sync.sync_worker.stop()
        if im.manager is not None:
            await im.manager.shutdown()
        logger.info("EMUNEL API shut down cleanly")


app = FastAPI(
    title="EMUNEL API",
    description="Premium multi-protocol proxy management platform API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Correlation-id middleware (auth stays at route dependencies)
app.add_middleware(AuthMiddleware)

# API v1 routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/api/v1/users", tags=["Users"])
app.include_router(nodes.router, prefix="/api/v1/nodes", tags=["Nodes"])
app.include_router(instances.router, prefix="/api/v1/instances", tags=["Instances"])
app.include_router(
    subscriptions.router, prefix="/api/v1/subscriptions", tags=["Subscriptions"]
)
app.include_router(traffic.router, prefix="/api/v1/traffic", tags=["Traffic"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(connections.router, prefix="/api/v1/connections", tags=["Online Connections"])
app.include_router(
    network_tests.router, prefix="/api/v1/network-tests", tags=["Network Tests"]
)
app.include_router(logs.router, prefix="/api/v1/logs", tags=["Logs"])
app.include_router(
    settings_router.router, prefix="/api/v1/settings", tags=["Settings"]
)
app.include_router(health_router.router, prefix="/api/v1/health", tags=["Health"])


# ---------------------------------------------------------------------------
# Public subscription feed (token-authenticated, like the reference)
# ---------------------------------------------------------------------------

@app.get("/sub/{link_token}", tags=["Subscription"])
async def subscription_feed(link_token: str, request: Request):
    """Base64 subscription body for proxy clients + profile headers.

    Only links whose instance is running and whose policy allows them are
    included. Credentials never appear beyond the share URLs themselves.
    """
    from .services.core_client import CoreClient, CoreUnavailable

    async with async_session() as db:
        sub = (await db.execute(
            select(Subscription).where(Subscription.link_token == link_token)
        )).scalar_one_or_none()
        if sub is None:
            raise HTTPException(404, detail="subscription not found")

        links = (await db.execute(
            select(Link).where(Link.subscription_id == sub.id)
        )).scalars().all()

        share_urls: list[str] = []
        used = 0
        for link in links:
            used += link.used_bytes
            inst = (await db.execute(
                select(Instance).where(Instance.id == link.instance_id)
            )).scalar_one_or_none()
            if inst is None or inst.status != InstanceStatus.RUNNING or not inst.core_port:
                continue
            host = inst.public_host or request.headers.get("host") or request.url.netloc
            try:
                client = CoreClient(inst.core_port, inst.core_api_token)
                out = await client.share_links(host, uuids=[link.uuid])
                share_urls.extend(item["share_url"] for item in out if item.get("share_url"))
            except CoreUnavailable:
                continue

        body = base64.b64encode("\n".join(share_urls).encode()).decode()
        total = sub.traffic_limit_bytes or 0
        expire_ts = int(sub.expires_at.timestamp()) if sub.expires_at else 0
        title_b64 = base64.b64encode(f"EMUNEL - {sub.name}".encode()).decode()
        return Response(
            content=body,
            media_type="text/plain",
            headers={
                "profile-title": f"base64:{title_b64}",
                "subscription-userinfo": f"upload=0; download={used}; total={total}; expire={expire_ts}",
                "profile-update-interval": "6",
                "content-disposition": 'attachment; filename="emunel-sub.txt"',
            },
        )


# ---------------------------------------------------------------------------
# Health + root
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"])
async def health():
    """Liveness probe (unauthenticated by design)."""
    return {"status": "ok", "service": "emunel-api", "version": "1.0.0"}


@app.get("/ready", tags=["Health"])
async def ready():
    from .services import health as health_svc
    from .database import async_session

    async with async_session() as db:
        db_health = await health_svc.database_health(db)
    return {"ready": db_health["status"] == "ok", "database": db_health}


@app.get("/api", tags=["Root"])
async def api_root():
    return {
        "name": "EMUNEL",
        "version": "1.0.0",
        "docs": "/api/docs",
        "health": "/health",
    }


# ---------------------------------------------------------------------------
# Dashboard SPA (vanilla static files, no build step)
# ---------------------------------------------------------------------------

if DASHBOARD_DIR.exists():
    app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")
