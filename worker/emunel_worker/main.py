"""EMUNEL Worker — node agent.

Runs on every infrastructure node. Responsibilities:

* HTTP API for the EMUNEL Console (auth via shared worker token):
    POST /worker/api/instances/:id/launch     deploy a EMUNEL Core container
    POST /worker/api/instances/:id/stop
    POST /worker/api/instances/:id/restart
    POST /worker/api/instances/:id/remove
    GET  /worker/api/instances/:id/status     (+ health probe of the Core)
    GET  /worker/api/instances/:id/logs
    GET  /worker/api/instances/:id/proxy      reverse proxy into the instance
    GET  /worker/api/metrics                  node CPU/RAM/disk + capacity
* Heartbeat loop: reports node status/capacity to the Console so the
  scheduler can place deployments.
* Reverse proxying: the worker forwards instance traffic
  (``GET /worker/api/instances/:id/proxy``) — in production the edge proxy
  routes ``<name>-<id>.emunel.app`` directly to the instance's loopback port.

Isolation: instances run under the driver (Docker with hard limits, or the
process driver with rlimits for development). The worker never exposes the
Docker socket to instances.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
from pathlib import Path

import httpx
import psutil
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import registry
from .driver import BaseDriver, DriverError, LaunchSpec, select_driver
from .logging import get, setup_logging
from .version import info as worker_info

setup_logging(os.environ.get("EMUNEL_LOG_LEVEL", "info"))
log = get("runtime", "emunel.worker")

WORKER_TOKEN = os.environ.get("EMUNEL_WORKER_TOKEN", "")
CONSOLE_URL = os.environ.get("EMUNEL_CONSOLE_URL", "")
CONSOLE_HEARTBEAT_TOKEN = os.environ.get("EMUNEL_WORKER_HEARTBEAT_TOKEN", "")
DATA_ROOT = Path(os.environ.get("EMUNEL_WORKER_DATA", "/var/lib/emunel/instances"))
NODE_ID = os.environ.get("EMUNEL_NODE_ID", "local")
NODE_REGION = os.environ.get("EMUNEL_NODE_REGION", "local")
PUBLIC_BASE = os.environ.get("EMUNEL_PUBLIC_BASE", "")
HEARTBEAT_INTERVAL = float(os.environ.get("EMUNEL_WORKER_HEARTBEAT_SECONDS", "15"))

if not WORKER_TOKEN:
    raise RuntimeError("EMUNEL_WORKER_TOKEN must be set (shared secret with the Console)")

driver: BaseDriver
app = FastAPI(title="EMUNEL Worker", docs_url=False, redoc_url=False)

# ── storm hardening (reconnect-loop / OOM fix) ──────────────────────────────
# The proxy used to build a FRESH httpx.AsyncClient per request and rely on
# a Starlette BackgroundTask for cleanup. BackgroundTask does NOT run when
# the client disconnects mid-stream (the response coroutine is cancelled),
# so every aborted xHTTP/WS stream leaked a client + its connection pool —
# unbounded RSS growth under client reconnect storms. Two fixes:
#   * one shared bounded client per Core port (pool caps, loopback only)
#   * response streaming wrapped in a try/finally generator so aclose()
#     ALWAYS runs, on the normal AND the cancelled path
_CORE_CLIENT_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8)
_core_clients: "dict[int, httpx.AsyncClient]" = {}


def _core_client(port: int) -> httpx.AsyncClient:
    """Shared bounded client for one Core's loopback port."""
    client = _core_clients.get(port)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
            limits=_CORE_CLIENT_LIMITS,
        )
        _core_clients[port] = client
    return client


async def _close_core_clients() -> None:
    for client in list(_core_clients.values()):
        try:
            await client.aclose()
        except Exception:
            pass
    _core_clients.clear()


def _stream_core(upstream_resp):
    """aiter_raw wrapped so the upstream response is ALWAYS aclose()'d —
    BackgroundTask alone skips cleanup on client disconnect (see above)."""
    async def _gen():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            try:
                await upstream_resp.aclose()
            except Exception:
                pass
    return _gen()


_WS_MAX_CONNECTIONS = max(0, int(os.environ.get("EMUNEL_WS_MAX_CONNECTIONS", "60")))
_WS_MAX_FRAME_BYTES = int(os.environ.get("EMUNEL_WS_MAX_FRAME_BYTES", str(4 * 1024 * 1024)))
_WS_WRITE_TIMEOUT = float(os.environ.get("EMUNEL_WS_WRITE_TIMEOUT", "10"))
_MONITOR_SEC = float(os.environ.get("EMUNEL_MONITOR_SEC", "30"))


class _WsGuard:
    """Cap + counter for live ws-proxy relays (visible in the monitor line)."""

    def __init__(self, cap: int):
        self.cap = cap
        self.active = 0
        self.rejected = 0

    def acquire(self) -> bool:
        if 0 < self.cap <= self.active:
            self.rejected += 1
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


_ws_guard = _WsGuard(_WS_MAX_CONNECTIONS)


async def _bounded_send(aw) -> None:
    """Send with a timeout (websockets 16 has no write_timeout kwarg):
    a peer that stops reading gets cut instead of pinning the relay."""
    await asyncio.wait_for(aw, timeout=_WS_WRITE_TIMEOUT)


def require_worker_token(request: Request) -> None:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, WORKER_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


async def probe_core(port: int, token: str, timeout: float = 3.0) -> dict | None:
    """Health-probe a running Core. Returns its health payload or None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/health")
            if resp.status_code != 200:
                return None
            return resp.json()
    except (httpx.HTTPError, OSError):
        return None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "EMUNEL Worker", "version": worker_info()["version"], "node": NODE_ID}


@app.get("/ready")
async def ready():
    return {"ready": True}


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------
@app.post("/worker/api/instances/{instance_id}/launch")
async def launch_instance(instance_id: str, request: Request, _=Depends(require_worker_token)):
    body = await request.json()
    if driver.is_known(instance_id):
        # Idempotent (re)launch: redeploys hit this path. Stop the old process
        # but KEEP its data dir so links and state survive.
        try:
            await driver.stop(instance_id)
        except DriverError:
            pass
        driver.handles.pop(instance_id, None)
    spec = LaunchSpec(
        instance_id=instance_id,
        deployment_id=str(body.get("deployment_id") or ""),
        core_version=str(body.get("core_version") or "latest"),
        api_token=body.get("core_api_token") or secrets.token_urlsafe(24),
        public_host=str(body.get("public_host") or ""),
        cpu_limit=float(body.get("cpu_limit") or 0.5),
        memory_mb=int(body.get("memory_mb") or 256),
        max_processes=int(body.get("max_processes") or 128),
    )
    try:
        handle = await driver.launch(spec)
    except DriverError as exc:
        raise HTTPException(status_code=502, detail=f"launch failed: {exc}")
    registry.save_instance(instance_id, {
        "deployment_id": spec.deployment_id,
        "core_version": spec.core_version,
        "api_token": spec.api_token,
        "public_host": spec.public_host,
        "cpu_limit": spec.cpu_limit,
        "memory_mb": spec.memory_mb,
        "max_processes": spec.max_processes,
        "port": handle.port,
    })
    return {
        "ok": True,
        "instance_id": instance_id,
        "port": handle.port,
        "driver": driver.name,
        "core_api_token": spec.api_token,
    }


@app.post("/worker/api/instances/{instance_id}/stop")
async def stop_instance(instance_id: str, _=Depends(require_worker_token)):
    try:
        await driver.stop(instance_id)
    except DriverError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.post("/worker/api/instances/{instance_id}/restart")
async def restart_instance(instance_id: str, request: Request, _=Depends(require_worker_token)):
    """Stop the instance (if running) and launch it again with the supplied
    launch parameters. The Console passes the stored deployment spec."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if driver.is_known(instance_id):
        try:
            await driver.stop(instance_id)
        except DriverError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    spec = LaunchSpec(
        instance_id=instance_id,
        deployment_id=str(body.get("deployment_id") or ""),
        core_version=str(body.get("core_version") or "latest"),
        api_token=body.get("core_api_token") or secrets.token_urlsafe(24),
        public_host=str(body.get("public_host") or ""),
        cpu_limit=float(body.get("cpu_limit") or 0.5),
        memory_mb=int(body.get("memory_mb") or 256),
        max_processes=int(body.get("max_processes") or 128),
    )
    try:
        handle = await driver.launch(spec)
    except DriverError as exc:
        raise HTTPException(status_code=502, detail=f"restart failed: {exc}")
    registry.save_instance(instance_id, {
        "deployment_id": spec.deployment_id,
        "core_version": spec.core_version,
        "api_token": spec.api_token,
        "public_host": spec.public_host,
        "cpu_limit": spec.cpu_limit,
        "memory_mb": spec.memory_mb,
        "max_processes": spec.max_processes,
        "port": handle.port,
    })
    return {"ok": True, "instance_id": instance_id, "port": handle.port, "driver": driver.name}


@app.post("/worker/api/instances/{instance_id}/remove")
async def remove_instance(instance_id: str, _=Depends(require_worker_token)):
    try:
        await driver.remove(instance_id)
    except DriverError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    registry.remove(instance_id)
    return {"ok": True}


@app.get("/worker/api/instances/{instance_id}/status")
async def instance_status(instance_id: str, _=Depends(require_worker_token)):
    try:
        status = await driver.status(instance_id)
    except DriverError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if status.get("running"):
        probe = await probe_core(status["port"], "")  # health endpoint needs no token
        status["core_health"] = probe
        status["healthy"] = probe is not None
    return status


@app.get("/worker/api/instances/{instance_id}/logs")
async def instance_logs(instance_id: str, tail: int = 200, _=Depends(require_worker_token)):
    return {"logs": await driver.logs(instance_id, tail=max(1, min(tail, 1000)))}


@app.api_route("/worker/api/instances/{instance_id}/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def instance_proxy(instance_id: str, path: str, request: Request, _=Depends(require_worker_token)):
    """Internal reverse proxy into an instance's Core (used by the Console for
    its management API calls and by the edge proxy for public traffic)."""
    try:
        status = await driver.status(instance_id)
    except DriverError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not status.get("running"):
        raise HTTPException(status_code=502, detail="instance not running")
    port = status["port"]
    url = httpx.URL(path="/" + path, query=request.url.query.encode())
    headers = [(k, v) for (k, v) in request.headers.items()
               if k.lower() not in ("host", "content-length", "authorization")]
    # Translate auth: the worker token got us in; the Core expects its own
    # instance token (stored at launch time).
    handle = driver.handles.get(instance_id)
    if handle is not None and handle.meta.get("api_token"):
        headers.append(("Authorization", f"Bearer {handle.meta['api_token']}"))
    # Stream responses: xHTTP downlinks are long-lived streams that never
    # "complete" — buffering them (client.request) would hang and die.
    # Shared bounded client per Core port (was: one fresh client PER request,
    # leaked when the caller disconnected mid-stream).
    client = _core_client(port)
    try:
        up_req = client.build_request(
            request.method, url, headers=headers,
            # Stream the request body: xHTTP stream-up POSTs are infinite
            # uploads — buffering via request.body() would block forever and
            # stream-up configs would never connect.
            content=None if request.method in ("GET", "HEAD", "OPTIONS") else request.stream(),
        )
        upstream = await client.send(up_req, stream=True)
        return StreamingResponse(
            _stream_core(upstream),
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items()
                     if k.lower() not in ("content-length", "transfer-encoding", "connection")},
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="upstream unavailable")


@app.websocket("/worker/api/instances/{instance_id}/ws-proxy/{path:path}")
async def instance_ws_proxy(ws: WebSocket, instance_id: str, path: str):
    """WebSocket relay into an instance's EMUNEL Core (authenticated via the
    worker token passed as query param, since browsers cannot set headers
    on WebSocket connects)."""
    import websockets as ws_lib

    # Accept first so later rejections carry proper close codes instead of
    # Starlette's generic HTTP 403 rejection.
    await ws.accept()
    if not _ws_guard.acquire():
        log.warning("ws-proxy: connection cap reached (%d active) — rejecting",
                    _ws_guard.active)
        try:
            await ws.close(code=1013, reason="try again later")
        except Exception:
            pass
        return
    try:
        token = ws.query_params.get("token", "")
        if not token or not secrets.compare_digest(token, WORKER_TOKEN):
            await ws.close(code=1008, reason="unauthorized")
            return
        try:
            status = await driver.status(instance_id)
        except DriverError:
            await ws.close(code=1014, reason="unknown instance")
            return
        if not status.get("running"):
            await ws.close(code=1013, reason="instance not running")
            return

        upstream_url = f"ws://127.0.0.1:{status['port']}/{path}"
        headers = {k: v for k, v in ws.headers.items()
                   if k.lower() in ("x-forwarded-for", "x-real-ip", "user-agent")}
        try:
            async with ws_lib.connect(upstream_url, additional_headers=headers,
                                      max_size=_WS_MAX_FRAME_BYTES, max_queue=32,
                                      write_limit=1024 * 1024,
                                      ping_interval=20, ping_timeout=20) as upstream:
                async def c2u():
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                return
                            data = msg.get("bytes")
                            if data is not None:
                                await _bounded_send(upstream.send(data))
                            else:
                                text = msg.get("text")
                                if text is not None:
                                    await _bounded_send(upstream.send(text))
                    except Exception:
                        return

                async def u2c():
                    try:
                        async for message in upstream:
                            if isinstance(message, (bytes, bytearray)):
                                await _bounded_send(ws.send_bytes(bytes(message)))
                            else:
                                await _bounded_send(ws.send_text(message))
                    except Exception:
                        return

                done, pending = await asyncio.wait(
                    {asyncio.create_task(c2u()), asyncio.create_task(u2c())},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if pending:
                    # release their resources before returning (cancel alone
                    # left them half-open under reconnect storms)
                    await asyncio.gather(*pending, return_exceptions=True)
        except Exception:
            try:
                await ws.close(code=1014, reason="upstream unavailable")
            except Exception:
                pass
            return
        finally:
            try:
                await ws.close()
            except Exception:
                pass
    finally:
        _ws_guard.release()


# ---------------------------------------------------------------------------
# Node metrics + heartbeat
# ---------------------------------------------------------------------------
def node_metrics() -> dict:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(DATA_ROOT if DATA_ROOT.exists() else "/"))
    return {
        "node_id": NODE_ID,
        "region": NODE_REGION,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(logical=True),
        "mem_used_mb": round(vm.used / (1024 * 1024)),
        "mem_total_mb": round(vm.total / (1024 * 1024)),
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "instances": len(driver.known_instances()),
        "capacity_instances": int(os.environ.get("EMUNEL_NODE_CAPACITY", "20")),
        "driver": driver.name,
        "ts": time.time(),
    }


@app.get("/worker/api/metrics")
async def metrics(_=Depends(require_worker_token)):
    return node_metrics()


async def heartbeat_loop() -> None:
    if not CONSOLE_URL:
        log.info("heartbeat disabled (EMUNEL_CONSOLE_URL not set)")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                resp = await client.post(
                    f"{CONSOLE_URL}/api/internal/heartbeat",
                    json=node_metrics(),
                    headers={"Authorization": f"Bearer {CONSOLE_HEARTBEAT_TOKEN or WORKER_TOKEN}"},
                )
                if resp.status_code != 200:
                    log.warning("heartbeat rejected: %s", resp.status_code)
            except (httpx.HTTPError, OSError) as exc:
                log.warning("heartbeat failed: %s", type(exc).__name__)
            await asyncio.sleep(HEARTBEAT_INTERVAL)


async def monitor_loop() -> None:
    """Memory/conn heartbeat (default 30s) — one Railway log line per tick.

    Python has no V8 heap counters; the honest equivalents here are RSS
    (psutil), live asyncio tasks, open file descriptors and the WS relay
    count. A leak shows up as rss/tasks/ws climbing tick over tick."""
    while True:
        await asyncio.sleep(_MONITOR_SEC)
        try:
            proc = psutil.Process()
            mem = proc.memory_info()
            try:
                fds = proc.num_fds()
            except Exception:
                fds = -1
            log.info(
                "monitor: rss=%.0fMB vms=%.0fMB fds=%d threads=%d "
                "asyncio_tasks=%d ws_active=%d ws_rejected=%d core_clients=%d",
                mem.rss / 1048576, mem.vms / 1048576, fds, proc.num_threads(),
                len(asyncio.all_tasks()), _ws_guard.active, _ws_guard.rejected,
                len(_core_clients),
            )
        except Exception:
            pass


@app.on_event("startup")
async def _startup() -> None:
    global driver
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    driver, _name = select_driver(DATA_ROOT)
    log.info("EMUNEL Worker %s on node=%s region=%s driver=%s", worker_info()["version"], NODE_ID, NODE_REGION, driver.name)
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(_relaunch_known_instances())
    asyncio.create_task(monitor_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    await _close_core_clients()


async def _relaunch_known_instances() -> None:
    """Pod restarts kill managed Core processes; relaunch everything the
    registry knows about with identical credentials."""
    for iid, rec in registry.load().items():
        if driver.is_known(iid):
            continue
        try:
            spec = LaunchSpec(
                instance_id=iid,
                deployment_id=str(rec.get("deployment_id") or ""),
                core_version=str(rec.get("core_version") or "latest"),
                api_token=str(rec.get("api_token") or secrets.token_urlsafe(24)),
                public_host=str(rec.get("public_host") or ""),
                cpu_limit=float(rec.get("cpu_limit") or 0.5),
                memory_mb=int(rec.get("memory_mb") or 256),
                max_processes=int(rec.get("max_processes") or 128),
                port=int(rec.get("port") or 0) or None,
            )
            await driver.launch(spec)
            log.info("relaunched instance %s after restart", iid[:12])
        except Exception as exc:
            log.warning("relaunch failed for %s: %s", iid[:12], exc)
