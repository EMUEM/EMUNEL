"""Core host — ``python -m engines.core_host``.

Launches the UNMODIFIED EMUNEL Core with engines attached from the outside:

    client -> worker ws-proxy -> [engines wrapper] -> Core app

The wrapper:
  * installs ONE dial hook around ``asyncio.open_connection`` (the call
    every relay uses to reach destinations) that dispatches to the active
    dial-interested engines (PreConnect warm pool, Congestion measurement
    + socket tuning). Removed at shutdown; engines off -> no hook at all.
  * serves GET /engines/api/status (bearer EMUNEL_CORE_API_TOKEN, the same
    guard Core uses for /core/api/*) so the Console can show the core-side
    engine status through the existing worker proxy path.
  * keeps Core's own lifespan semantics (state load on boot, state + VMess
    runtime cleanup on shutdown) by chaining the same steps __main__ uses.

Never-crash contract: if the engines layer fails in ANY way, this host
falls back to serving the raw Core app (the platform stays up, engines
report dead). argv is exactly emunel_core's (``--port``, ``--host``,
``--state-path``, ``--log-level``, ``--config``, ``--public-host``).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
import time
from pathlib import Path


def _install_dial_hook(manager):
    """Wrap asyncio.open_connection once; dispatch to active engines."""
    original = asyncio.open_connection

    async def engines_open_connection(host=None, port=None, *args, **kwargs):
        started = time.monotonic()
        preconnect = manager.engines.get("PreConnect")
        if preconnect is not None and preconnect.status.active:
            warm = preconnect.take_warm(host, port)
            if warm is not None:
                return warm
        result = await original(host, port, *args, **kwargs)
        try:
            congestion = manager.engines.get("Congestion")
            if congestion is not None and congestion.status.active:
                writer = result[1] if isinstance(result, tuple) else None
                congestion.note_dial(time.monotonic() - started, writer)
        except Exception:
            pass
        return result

    asyncio.open_connection = engines_open_connection
    return original


def _remove_dial_hook(original) -> None:
    asyncio.open_connection = original


def _per_instance_data_dir(state_path: str) -> str:
    """The Core's OWN engine state dir: a sibling `engines/` directory of the
    Core's state file. Never the console's shared store — a Core's periodic
    state flush would otherwise overwrite the console's state.json (and vice
    versa), destroying engine toggles and metrics on both sides."""
    from pathlib import Path

    return str(Path(state_path).resolve().parent / "engines")


class _CoreHostASGI:
    """ASGI callable: status route here, everything else to the Core app."""

    def __init__(self, app, manager, api_token: str):
        self.app = app
        self.manager = manager
        self.api_token = api_token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "") == "/engines/api/status":
            await self._status(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _status(self, scope, receive, send) -> None:
        # same bearer guard Core uses for /core/api/*
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        token = (headers.get("authorization") or "").removeprefix("Bearer ").strip()
        body = b""
        if not self.api_token or not token or not secrets.compare_digest(token, self.api_token):
            body = json.dumps({"detail": "unauthorized"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        payload = json.dumps(self.manager.status(), default=str).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})


def main(argv: list[str] | None = None) -> int:
    from emunel_core.app import Core
    from emunel_core.config import build_config
    from emunel_core.logging import get, setup_logging

    try:
        cfg, _ns = build_config(argv)
    except (ValueError, FileNotFoundError) as exc:
        print(f"emunel-core-host: configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_level, cfg.log_json)
    log = get("runtime", "emunel.core.host")

    core = Core(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await core.store.load(core.links, core.stats)
        from emunel_core.version import info

        log.info(
            "EMUNEL Core (engines host) %s listening on %s:%d",
            info()["version"], cfg.host, cfg.port,
        )
        try:
            yield
        finally:
            await core.vmess_runtime.close()
            await core.store.save(core.links, core.stats)
        log.info("EMUNEL Core shut down cleanly")

    core.app.router.lifespan_context = lifespan

    app = core.app
    original_dial = None
    manager = None
    try:
        from .config import get_env
        from .manager import EngineManager

        ecfg = get_env("core")
        if ecfg.enabled:
            # NEVER share the console's engine state: the Core gets its own
            # store under the instance's data dir (EMUNEL_STATE_PATH's parent).
            # The worker already points EMUNEL_ENGINE_DATA there; this is the
            # guarantee for every other launch style.
            try:
                per_instance = _per_instance_data_dir(cfg.state_path)
                Path(per_instance).mkdir(parents=True, exist_ok=True)
                ecfg.data_dir = per_instance
            except (OSError, ValueError):
                pass
            manager = EngineManager("core", cfg=ecfg)

            @contextlib.asynccontextmanager
            async def engines_lifespan(app_):
                await manager.start()
                try:
                    async with lifespan(app_):
                        yield
                finally:
                    await manager.stop()

            core.app.router.lifespan_context = engines_lifespan
            app = _CoreHostASGI(core.app, manager, cfg.api_token)
            # the dial hook must exist before any relay dials: install now,
            # remove on interpreter exit (child-process scope, no leaks)
            original_dial = _install_dial_hook(manager)
            log.info("engines attached: %s", ",".join(manager.cfg.pipeline_order))
        else:
            log.info("engines disabled by env — serving the raw Core")
    except Exception as exc:  # never-crash: raw core on any engine failure
        log.error("engines could not attach (%s) — serving the raw Core", exc)
        app = core.app

    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level,
                workers=1, loop="auto", ws="auto")
    if original_dial is not None:
        _remove_dial_hook(original_dial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
