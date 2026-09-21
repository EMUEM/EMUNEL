"""Evolution API — /api/{mesh,chaos,genetic,synergy}/* (NEW, additive).

Mirrors the panel's admin conventions exactly like engines/api.py does
(console session + admin flag + CSRF on privileged mutations), and adds
nothing to the existing surface. Two mesh endpoints are intentionally
public (they are what CLIENTS call to share DPI signatures / fetch a
policy — they carry no session and no PII, and are rate-limited both by
the console limiter and in-engine):

  GET  /api/mesh/policy?isp=X&region=Y      public  — policy lookup
  POST /api/mesh/report                      public  — submit a signature

  GET  /api/chaos/status                     admin — window/frame/self-play
  GET  /api/genetic/population               admin — the genome table
  GET  /api/genetic/status                    admin — generations + timers
  POST /api/genetic/evolve                   admin — Force Evolution
  GET  /api/synergy/status                   admin — flags + engine states +
                                                  the DpiMesh map (Evolution
                                                  tab visibility source)
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def build_evolution_router(manager) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["evolution"],
                       include_in_schema=False)

    # ---- auth (same guard as /api/engines/*) ------------------------------
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

    def _engine(name: str):
        return manager.engines.get(name)

    def _disabled(name: str):
        engine = _engine(name)
        reason = engine.status.reason if engine is not None else "not running"
        return JSONResponse({"enabled": False, "active": False,
                             "reason": reason}, status_code=200)

    def _flags() -> dict:
        cfg = manager.cfg
        return {
            "chaos": bool(cfg.chaos_on),
            "mesh": bool(cfg.mesh_on),
            "genetic": bool(cfg.genetic_on),
            "synergy": bool(cfg.synergy_on),
        }

    def _any_evolution_active() -> bool:
        return any(_engine(n) is not None and _engine(n).status.active
                   for n in ("Chaos", "Mesh", "Genetic", "Synergy"))

    # ---- DpiMesh (public, privacy-preserving) --------------------------------
    @router.get("/mesh/policy")
    async def mesh_policy(request: Request, isp: str = "",
                          region: str = ""):
        engine = _engine("Mesh")
        if engine is None or not engine.status.active:
            return {"enabled": False, "policy": None,
                    "reason": "DpiMesh disabled (DPI_MESH_ENABLED=false)"}
        if not isp or not region:
            return JSONResponse(
                {"detail": "isp and region query params required"},
                status_code=400)
        return engine.policy(isp=isp, region=region)

    @router.post("/mesh/report")
    async def mesh_report(request: Request):
        engine = _engine("Mesh")
        if engine is None or not engine.status.active:
            # graceful: clients treat reports as fire-and-forget, a 503
            # would only teach them to retry-spam
            return {"stored": False, "enabled": False,
                    "reason": "DpiMesh disabled (DPI_MESH_ENABLED=false)"}
        raw = await request.body()
        if len(raw) > 1024:
            return JSONResponse({"detail": "report too large (max 1KB)"},
                                status_code=413)
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except ValueError:
            return JSONResponse({"detail": "invalid JSON body"},
                                status_code=400)
        # never echo unvalidated content back
        isp = str(body.get("isp") or "")[:64]
        region = str(body.get("region") or "")[:64]
        if not isp or not region:
            return JSONResponse(
                {"detail": "isp and region required"}, status_code=400)
        try:
            latency = body.get("latency")
            if latency is not None:
                latency = float(latency)
        except (TypeError, ValueError):
            latency = None
        return engine.report(
            isp=isp, region=region,
            protocol=str(body.get("protocol") or "")[:64],
            transport=str(body.get("transport") or "")[:64],
            sni=str(body.get("sni") or "")[:64],
            result=str(body.get("result") or "ok")[:64],
            latency=latency)

    # ---- Chaos -----------------------------------------------------------------
    @router.get("/chaos/status")
    async def chaos_status(request: Request):
        await _admin(request)
        engine = _engine("Chaos")
        if engine is None or not engine.status.active:
            return _disabled("Chaos")
        out = engine.snapshot_metrics()
        out["enabled"] = True
        out["active"] = True
        out["genome"] = engine.hints()
        return out

    # ---- Genetic ------------------------------------------------------------------
    @router.get("/genetic/population")
    async def genetic_population(request: Request):
        await _admin(request)
        engine = _engine("Genetic")
        if engine is None or not engine.status.active:
            return _disabled("Genetic")
        return {
            "enabled": True, "active": True,
            "generation": engine.generation,
            "size": len(engine.population),
            "population": engine.population_payload(),
        }

    @router.get("/genetic/status")
    async def genetic_status(request: Request):
        await _admin(request)
        engine = _engine("Genetic")
        if engine is None or not engine.status.active:
            return _disabled("Genetic")
        best = engine.best_genome() or {}
        return {
            "enabled": True, "active": True,
            "profile": engine.defaults(),
            "metrics": engine.snapshot_metrics(),
            "generation": engine.generation,
            "size": len(engine.population),
            "best": best.get("id"),
            "best_fitness": best.get("fitness"),
            "history": engine.history(),
        }

    @router.post("/genetic/evolve")
    async def genetic_evolve(request: Request):
        await _admin(request)
        engine = _engine("Genetic")
        if engine is None or not engine.status.active:
            return _disabled("Genetic")
        return engine.evolve(reason="manual")

    # ---- Synergy ---------------------------------------------------------------------
    @router.get("/synergy/status")
    async def synergy_status(request: Request):
        await _admin(request)
        engines: dict = {}
        for name in ("Chaos", "Mesh", "Genetic", "Synergy"):
            engine = _engine(name)
            engines[name] = {
                "enabled": bool(engine and engine.status.enabled),
                "active": bool(engine and engine.status.active),
                "reason": engine.status.reason if engine else "not running",
            }
        payload = {
            "flags": _flags(),
            "any_active": _any_evolution_active(),
            "engines": engines,
            "mesh_map": [],
            "cycle": None,
        }
        mesh = _engine("Mesh")
        if mesh is not None and mesh.status.active:
            payload["mesh_map"] = mesh.policies(limit=50)
        syn = _engine("Synergy")
        if syn is not None and syn.status.active:
            payload["cycle"] = syn.snapshot_metrics()
        return payload

    return router
