"""Console-side engines API — /api/engines/* (NEW namespace, nothing else moves).

Routes mirror the panel's admin conventions (session + admin flag + CSRF
on mutations) and are additive: the existing /api/* surface is untouched.

  GET  /api/engines                     status of every engine (console host
                                        + best-effort per-instance core host
                                        through the existing worker proxy)
  POST /api/engines/{name}/enable       hot-enable an engine
  POST /api/engines/{name}/disable      hot-disable an engine
  GET  /api/engines/logs?name=&limit=   per-engine recent log lines
  POST /api/engines/selftest            passthrough + codec self-checks

  GET  /api/engines/sni/status          bypass profile + metrics + helper usage
  POST /api/engines/sni/config          update the profile (key-presence)
  POST /api/engines/sni/restart         full reload (env + persisted profile)
  POST /api/engines/sni/test            server-side fragment-plan proof
  GET  /api/engines/sni/helper          the client-side helper script text

  GET  /api/engines/reality/status      profile + keypair + runtime state
  POST /api/engines/reality/config      update the profile (key-presence)
  POST /api/engines/reality/keys        generate a fresh X25519 keypair
  POST /api/engines/reality/restart     reload keys/env + cycle the runtime
  POST /api/engines/reality/generate    inbound + outbound + vless:// link
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request

from .base import KIND_CONFIGGEN, KIND_FRAMES

_cache: dict = {"at": 0.0, "cores": []}
_skip_until: dict[str, float] = {}   # instance_id -> monotonic ts to re-probe


def build_router(manager) -> APIRouter:
    router = APIRouter(prefix="/api/engines", tags=["engines"],
                       include_in_schema=False)

    # ---- auth (console session, admin-only — same guard as /api/admin/*) ----
    async def _admin(request: Request):
        from emunel_console.auth import sessions

        pool = _pool(request)
        user = await sessions.get_session_user(pool, request)
        sessions.require_admin(user)
        sessions.check_csrf(request, user)
        return user

    def _pool(request: Request):
        from emunel_console.db import get_pool

        return get_pool(request)

    async def _core_engine_status(request: Request) -> list[dict]:
        """Best-effort per-instance core-side engine status through the
        existing worker proxy path.

        Sequential 2.5s worker calls made /api/engines crawl (the panel polls
        it) whenever a Core was slow — now every instance is probed in
        PARALLEL with a short timeout, results are cached for a full minute
        and instances that just failed are skipped for a cool-off period."""
        now = time.monotonic()
        cache_ttl = max(15.0, manager.cfg.status_cache_sec * 4)
        if now - _cache["at"] < cache_ttl:
            return _cache["cores"]

        async def _one(row) -> dict | None:
            instance_id = str(row["id"])
            try:
                node_url = await _node_url(pool, instance_id)
                status = await worker_svc.worker_call(
                    node_url, "GET",
                    f"/worker/api/instances/{instance_id}/proxy/engines/api/status",
                    timeout=1.5,
                )
                if isinstance(status, dict):
                    _skip_until.pop(instance_id, None)
                    return {
                        "instance_id": instance_id,
                        "instance_name": row["name"],
                        "engines": status.get("engines", []),
                        "host": status.get("host", "core"),
                    }
            except Exception:
                pass
            # unreachable core: stop probing it for two minutes
            _skip_until[instance_id] = now + 120.0
            return None

        cores: list[dict] = []
        try:
            from emunel_console.services import workers as worker_svc
            from emunel_console.services.links import _node_url

            pool = _pool(request)
            rows = await pool.fetch(
                "SELECT id, name FROM instances WHERE status = 'running' LIMIT 25")
            rows = [r for r in rows if _skip_until.get(str(r["id"]), 0.0) < now]
            if rows:
                results = await asyncio.gather(*[_one(r) for r in rows])
                cores = [c for c in results if c]
        except Exception:
            cores = []
        _cache["at"] = now
        _cache["cores"] = cores
        return cores

    @router.get("")
    @router.get("/")
    async def engines_status(request: Request, _=None):
        await _admin(request)
        payload = manager.status()
        payload["cores"] = await _core_engine_status(request)
        payload["api"] = {"version": 1}
        return payload

    # ---- SNI Spoofing (bypass profile generator + client helper) ------------
    def _engine(name: str):
        engine = manager.engines.get(name)
        if engine is None or not engine.status.active:
            from fastapi import HTTPException

            reason = engine.status.reason if engine is not None else "not running"
            raise HTTPException(
                status_code=503,
                detail=f"{name} engine is not active: {reason}")
        return engine

    @router.get("/sni/status")
    async def sni_status(request: Request, _=None):
        await _admin(request)
        engine = _engine("SNISpoof")
        return {
            "profile": engine.defaults(),
            "metrics": engine.snapshot_metrics(),
            "helper_usage": engine.helper_usage(),
            "methods": ["fragment", "fake_sni", "combined"],
            "strategies": ["sni_split", "half", "multi", "tls_record_frag"],
        }

    @router.post("/sni/config")
    async def sni_config(request: Request, _=None):
        await _admin(request)
        engine = _engine("SNISpoof")
        body = await request.json()
        try:
            profile = engine.set_profile(body)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "profile": profile}

    @router.post("/sni/restart")
    async def sni_restart(request: Request, _=None):
        await _admin(request)
        # full manager cycle: stop -> re-activate (re-reads env + persisted profile)
        await manager.set_engine_enabled("SNISpoof", False)
        ok, message = await manager.set_engine_enabled("SNISpoof", True)
        return {"ok": ok, "message": message or "SNI profile generator reloaded"}

    @router.post("/sni/test")
    async def sni_test(request: Request, _=None):
        await _admin(request)
        engine = _engine("SNISpoof")
        return engine.run_test()

    @router.get("/sni/helper")
    async def sni_helper(request: Request, download: int = 0, _=None):
        await _admin(request)
        engine = _engine("SNISpoof")
        source = engine.helper_source()
        headers = {"Cache-Control": "no-store"}
        if download:
            headers["Content-Disposition"] = 'attachment; filename="emunel_sni_helper.py"'
        from fastapi import Response

        return Response(content=source, media_type="text/plain; charset=utf-8",
                        headers=headers)

    # ---- REALITY (keypair + config generator + optional runtime) ------------
    @router.get("/reality/status")
    async def reality_status(request: Request, _=None):
        await _admin(request)
        engine = _engine("Reality")
        public = engine.active_public_key()
        return {
            "profile": engine.defaults(),
            "public_key": public,
            "keypair_present": bool(public),
            "client_uuid": engine.active_client_uuid(),
            "runtime": {
                "configured": engine.runtime_configured(),
                "running": engine.runtime_running(),
                "listen": (f"{engine.cfg.reality_listen_host}:"
                           f"{engine.profile['listen_port']}"),
                "xray_binary": engine.cfg.xray_binary or "",
                "how_to_enable": "install an Xray release, set EMUNEL_XRAY_BINARY "
                                  "(absolute path) + EMUNEL_XRAY_SHA256 (digest), "
                                  "expose EMUNEL_REALITY_LISTEN_PORT via a Railway "
                                  "TCP Proxy — see docs/RAILWAY.md",
            },
            "metrics": engine.snapshot_metrics(),
            "transports": ["raw", "xhttp", "grpc"],
        }

    @router.post("/reality/config")
    async def reality_config(request: Request, _=None):
        await _admin(request)
        engine = _engine("Reality")
        body = await request.json()
        try:
            profile = engine.set_profile(body)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "profile": profile}

    @router.post("/reality/keys")
    async def reality_keys(request: Request, _=None):
        await _admin(request)
        engine = _engine("Reality")
        return engine.generate_keys()

    @router.post("/reality/restart")
    async def reality_restart(request: Request, _=None):
        await _admin(request)
        # full manager cycle: stops any running Xray runtime, re-reads env
        # (keys, binary pin) and starts the runtime again when configured
        await manager.set_engine_enabled("Reality", False)
        ok, message = await manager.set_engine_enabled("Reality", True)
        engine = manager.engines.get("Reality")
        runtime = {"running": bool(engine and engine.runtime_running()),
                   "configured": bool(engine and engine.runtime_configured())} \
            if engine else {"running": False, "configured": False}
        return {"ok": ok, "message": message or "REALITY engine reloaded",
                "runtime": runtime}

    @router.post("/reality/generate")
    async def reality_generate(request: Request, _=None):
        """Build inbound + outbound + vless:// link for the asking client.
        Body: {transport?: raw|xhttp|grpc, host?: public address (default
        request Host), port?: override the listen port}."""
        await _admin(request)
        engine = _engine("Reality")
        body = await request.json() if request.headers.get("content-type") else {}
        address = str(body.get("host") or request.headers.get("host") or "").split(":")[0]
        if not address:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail="host required (body or Host header)")
        transport = str(body.get("transport") or "raw")
        try:
            port = int(body.get("port") or 0) or None
        except (TypeError, ValueError):
            port = None
        try:
            return engine.generate_configs(address=address, transport=transport, port=port)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/{name}/enable")
    async def engines_enable(name: str, request: Request, _=None):
        await _admin(request)
        ok, message = await manager.set_engine_enabled(name, True)
        return {"ok": ok, "message": message, "status": manager.status().get("engines")}

    @router.post("/{name}/disable")
    async def engines_disable(name: str, request: Request, _=None):
        await _admin(request)
        ok, message = await manager.set_engine_enabled(name, False)
        return {"ok": ok, "message": message, "status": manager.status().get("engines")}

    @router.get("/logs")
    async def engines_logs(request: Request, name: str = "", limit: int = 80, _=None):
        await _admin(request)
        if not name:
            return {"logs": []}
        limit = max(1, min(limit, 300))
        return {"logs": manager.engine_logs(name, limit)}

    @router.post("/selftest")
    async def engines_selftest(request: Request, _=None):
        await _admin(request)
        results: dict = {}
        # pipeline passthrough: an empty frame batch must come back empty
        from .base import EngineContext

        ctx = EngineContext(kind=KIND_FRAMES, hop="client", direction="down",
                            frames=[], meta={})
        out = await manager.run_pipeline(ctx)
        results["passthrough_empty_batch"] = (out.frames == [])
        # configgen passthrough with an unparseable body
        ctx = EngineContext(kind=KIND_CONFIGGEN, hop="client",
                            meta={"format": "raw", "body": "!!!not-base64!!!",
                                  "headers": [], "path": "/i/x/sub"})
        out = await manager.run_pipeline(ctx)
        results["configgen_defensive"] = (out.meta.get("body") == "!!!not-base64!!!")
        # codec roundtrips
        try:
            from .engines.fec import decode_group, encode_group

            data = [b"abc", b"defg", b"h"]
            enc = encode_group(data, parity_count=1)
            holes = list(enc)
            holes[0] = None
            rec = decode_group(holes, parity_count=1)
            results["fec_codec"] = bool(rec and rec[0][:3] == b"abc")
        except Exception as exc:
            results["fec_codec"] = f"error: {exc}"
        try:
            import zlib

            blob = b"compressible " * 64
            results["zlib_roundtrip"] = (
                zlib.decompress(zlib.compress(blob, 6)) == blob)
        except Exception as exc:
            results["zlib_roundtrip"] = f"error: {exc}"
        results["all_ok"] = all(v is True for k, v in results.items() if k != "all_ok")
        return results

    return router
