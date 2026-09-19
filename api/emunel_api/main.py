"""EMUNEL API — FastAPI application."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .middleware.auth import AuthMiddleware
from .routers.v1 import (
    auth,
    users,
    nodes,
    subscriptions,
    traffic,
    analytics,
    network_tests,
    logs,
    settings as settings_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Application lifespan: startup and shutdown."""
    settings.validate_runtime()
    await init_db()
    yield


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

# Auth middleware
app.add_middleware(AuthMiddleware)

# API v1 routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/api/v1/users", tags=["Users"])
app.include_router(nodes.router, prefix="/api/v1/nodes", tags=["Nodes"])
app.include_router(
    subscriptions.router, prefix="/api/v1/subscriptions", tags=["Subscriptions"]
)
app.include_router(traffic.router, prefix="/api/v1/traffic", tags=["Traffic"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(
    network_tests.router, prefix="/api/v1/network-tests", tags=["Network Tests"]
)
app.include_router(logs.router, prefix="/api/v1/logs", tags=["Logs"])
app.include_router(
    settings_router.router, prefix="/api/v1/settings", tags=["Settings"]
)


@app.get("/health", tags=["Health"])
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "emunel-api", "version": "1.0.0"}


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint — redirects to docs."""
    return {
        "name": "EMUNEL API",
        "version": "1.0.0",
        "docs": "/api/docs",
        "health": "/health",
    }
