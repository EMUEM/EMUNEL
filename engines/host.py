"""Console host wiring — wrap the Console app with engines (main.py calls this).

wrap_console(app) returns either:
  * the SAME app untouched — when engines are disabled by env or the
    pipeline order is empty (bit-for-bit old behaviour: no middleware, no
    extra routes, nothing), or
  * the app equipped with (a) the /api/engines/* router (new namespace
    only), (b) a chained lifespan that starts/stops the engine manager,
    and (c) the ASGI middleware for the gateway hop, subscription feeds
    and probe defense.

This module never imports console code at import time — console modules
are imported lazily inside the function so the engines package stays
loadable in the Core process (and in tests) without the console present.
"""
from __future__ import annotations

import contextlib


def wrap_console(console_app, manager=None):
    from .config import get_env

    cfg = get_env("console")
    if not cfg.enabled:
        return console_app
    # only activate core-side engine launching when a core engine is on;
    # with no core-side engines the worker launches Core exactly as before
    _maybe_enable_core_host(cfg)

    if manager is None:
        from .manager import EngineManager

        manager = EngineManager("console", cfg=cfg)

    # panel page (for probe-response comparison) — best effort
    panel_page: str | None = None
    try:
        from emunel_console.panel import PAGE

        panel_page = PAGE
    except Exception:
        panel_page = None

    # 1. /api/engines/* router — additive namespace
    try:
        from .api import build_router

        console_app.include_router(build_router(manager))
    except Exception as exc:
        print(f"[emunel-engines] WARNING: /api/engines router not mounted: {exc}",
              flush=True)

    # 2. chain the lifespan: engine manager start/stop around the console's
    try:
        original_lifespan = console_app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def engines_lifespan(app):
            await manager.start()
            try:
                async with original_lifespan(app):
                    yield
            finally:
                await manager.stop()

        console_app.router.lifespan_context = engines_lifespan
    except Exception as exc:
        print(f"[emunel-engines] WARNING: lifespan chaining failed: {exc}", flush=True)

    # 3. ASGI middleware around everything
    from .middleware import EnginesASGIMiddleware

    return EnginesASGIMiddleware(console_app, manager, cfg, panel_page=panel_page)


def _maybe_enable_core_host(cfg) -> None:
    """Ask the worker to launch Core through engines.core_host when any
    core-host engine is active. Done purely via env vars the worker already
    honours (EMUNEL_CORE_MODULE) — default stays the raw core module."""
    import os

    core_engines = ("PreConnect", "Congestion", "Compress", "FEC")
    any_core_engine = False
    for name in core_engines:
        key = f"{name.lower()}_on"
        if name in (cfg.pipeline_order or []) and getattr(cfg, key, False):
            any_core_engine = True
    if not any_core_engine:
        return
    root = os.environ.get("EMUNEL_ENGINES_ROOT") or _repo_root()
    if root:
        os.environ["EMUNEL_CORE_MODULE"] = "engines.core_host"
        os.environ["EMUNEL_ENGINES_ROOT"] = str(root)


def _repo_root() -> str:
    import os
    from pathlib import Path

    cwd = os.environ.get("EMUNEL_CORE_CWD", "")
    if cwd:
        return str(Path(cwd).resolve().parent)
    return str(Path(__file__).resolve().parents[1])
