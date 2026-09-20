"""EMUNEL Console API — ASGI application.

Serves:
* the Console API under /api and /auth
* the public instance gateway at /i/<token>/... (HTTP + WebSocket)
* the static frontend (console/frontend) for everything else (SPA)
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import version
from .db import close_db, init_pool
from .logging import get, setup_logging
from .panel import router as panel_router
from .routers import admin, auth, backup, domains, instances, internal
from .security.ratelimit import RateLimitASGI
from .services.gateway import router as gateway_router

setup_logging()
log = get("runtime", "emunel.console")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="EMUNEL Console", docs_url=None, redoc_url=None,
              version=version.version())

# Control-plane rate limiting (auth + panel APIs only — never the /i/* proxy
# gateway; see RateLimitASGI for why). Pure ASGI, streaming-safe.
app.add_middleware(RateLimitASGI)

app.include_router(panel_router)
app.include_router(auth.router)
app.include_router(instances.router)
app.include_router(domains.router)
app.include_router(admin.router)
app.include_router(backup.router)
app.include_router(internal.router)
app.include_router(gateway_router)


@app.get("/", include_in_schema=False)
async def panel_home():
    from .panel import PAGE

    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "EMUNEL Console",
            "version": version.version(), "build": version.build()}


@app.get("/ready")
async def ready():
    from .db import db

    return {"ready": db is not None, "backend": getattr(db, "mode", None)}


@app.get("/version")
async def version_endpoint():
    return version.info()


@app.exception_handler(404)
async def spa_fallback(request, exc):
    """Serve the self-contained panel for unknown browser paths."""
    path = request.url.path
    if path.startswith(("/api", "/auth", "/i/", "/worker")) or path == "/health":
        return JSONResponse({"detail": "not found"}, status_code=404)
    from .panel import PAGE

    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})


@contextlib.asynccontextmanager
async def lifespan(_app):
    await init_pool()
    # Volume-limit enforcement (only acts on instances with a cap set).
    from . import db as _db
    from .services.volume import enforcement_loop

    volume_task = None
    try:
        volume_task = asyncio.create_task(enforcement_loop(_db.db))
    except Exception as exc:  # never block startup
        log.warning("volume enforcement loop not started: %s", exc)
    log.info("EMUNEL Console %s (build %s) started", version.version(), version.build())
    yield
    if volume_task is not None:
        volume_task.cancel()
    await close_db()
    log.info("EMUNEL Console stopped")


app.router.lifespan_context = lifespan

if (FRONTEND_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")
