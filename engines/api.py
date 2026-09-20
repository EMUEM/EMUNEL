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
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from .base import KIND_CONFIGGEN, KIND_FRAMES

_cache: dict = {"at": 0.0, "cores": []}


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
        existing worker proxy path (cached; never blocks the response)."""
        now = time.monotonic()
        if now - _cache["at"] < max(3.0, manager.cfg.status_cache_sec):
            return _cache["cores"]
        cores: list[dict] = []
        try:
            from emunel_console.services import workers as worker_svc
            from emunel_console.services.links import _node_url

            pool = _pool(request)
            rows = await pool.fetch(
                "SELECT id, name FROM instances WHERE status = 'running' LIMIT 25")
            for row in rows:
                instance_id = str(row["id"])
                try:
                    node_url = await _node_url(pool, instance_id)
                    status = await worker_svc.worker_call(
                        node_url, "GET",
                        f"/worker/api/instances/{instance_id}/proxy/engines/api/status",
                        timeout=2.5,
                    )
                    if isinstance(status, dict):
                        cores.append({
                            "instance_id": instance_id,
                            "instance_name": row["name"],
                            "engines": status.get("engines", []),
                            "host": status.get("host", "core"),
                        })
                except Exception:
                    continue
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
