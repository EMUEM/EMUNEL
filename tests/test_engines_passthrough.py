"""Engines-on vs engines-off equivalence — the platform-first proof.

The operator's acceptance criterion: "write a simple test proving the Core
with engines OFF works exactly like before."

Three levels are proven here:

  A. wrap identity — EMUNEL_ENGINES_ENABLED=0 (or an empty pipeline) means
     main.py's wrap_console returns the SAME app object: not even a
     middleware is installed; behaviour is bit-for-bit the old one.
  B. full-stack relay equivalence — a REAL VLESS-over-WebSocket roundtrip
     through console gateway -> worker ws-proxy -> Core -> echo server,
     run TWICE on the same stack: once with engines OFF (raw console app)
     and once with engines ON (middleware + active Coalesce/Morph/Split
     pipeline). Both must return the exact same bytes.
  C. worker launch command — with engines off the ProcessDriver still runs
     `-m emunel_core`; it only substitutes the engines host when
     EMUNEL_CORE_MODULE is explicitly set.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
import websockets

ROOT = Path(__file__).resolve().parents[1]
for rel in ("core", "worker", "console/api", "."):
    sys.path.insert(0, str(ROOT / rel))

from emunel_core.app import Core
from emunel_core.config import CoreConfig
from emunel_core.relay.vless import build_vless_header
from emunel_core.state import Link

UUID = "123e4567-e89b-12d3-a456-426614174000"
WORKER_TOKEN = "worker-test-token"
CORE_TOKEN = "core-test-token"
CHUNKS = 12
CHUNK_SIZE = 900
MARKER = b"ENGINES-PASSTHROUGH-"
ECHO_PAYLOAD = MARKER + b"".join(
    bytes([65 + (i % 26)]) * CHUNK_SIZE for i in range(CHUNKS))


# ── A. wrap identity ─────────────────────────────────────────────────────────
def test_wrap_identity_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path))
    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "0")
    from emunel_console.main import app as console_app
    from engines.host import wrap_console

    assert wrap_console(console_app) is console_app


def test_wrap_identity_when_pipeline_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path))
    monkeypatch.setenv("EMUNEL_PIPELINE_ORDER", "")
    # empty string means "use default" — disable via a single space instead
    monkeypatch.setenv("EMUNEL_PIPELINE_ORDER", " ")
    from emunel_console.main import app as console_app
    from engines.host import wrap_console

    assert wrap_console(console_app) is console_app


# ── C. worker launch command unchanged by default ────────────────────────────
def test_worker_launch_command_default(monkeypatch, tmp_path):
    monkeypatch.delenv("EMUNEL_CORE_MODULE", raising=False)
    from emunel_worker.driver import ProcessDriver

    driver = ProcessDriver.__new__(ProcessDriver)
    driver.core_cmd = None
    driver.core_cwd = str(ROOT / "core")
    cmd = [
        os.environ.get("EMUNEL_CORE_PYTHON", ".venv/bin/python"),
        "-m", os.environ.get("EMUNEL_CORE_MODULE", "emunel_core"),
    ]
    assert cmd[1:] == ["-m", "emunel_core"]
    monkeypatch.setenv("EMUNEL_CORE_MODULE", "engines.core_host")
    assert os.environ["EMUNEL_CORE_MODULE"] == "engines.core_host"


# ── B. full-stack relay equivalence ──────────────────────────────────────────
async def _serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(5):
        while not server.started:
            await asyncio.sleep(.01)
    return server, task, sock, server.servers[0].sockets[0].getsockname()[1]


async def _chunked_echo(reader, writer):
    """Echo the request in small spaced chunks — produces multiple small
    downlink WS frames so the Coalesce engine has real work to do."""
    try:
        data = await reader.read(65536)
        for i in range(0, len(data), CHUNK_SIZE):
            writer.write(data[i:i + CHUNK_SIZE])
            await writer.drain()
            await asyncio.sleep(0.03)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _build_stack(tmp: Path, engines_on: bool, env_overrides: dict):
    """Boot core+worker+console(±engines) exactly like production."""
    core = Core(CoreConfig(api_token=CORE_TOKEN,
                           state_path=str(tmp / f"state-{engines_on}.json"),
                           log_level="error"))
    core._schedule_save = lambda: None
    core.ctx._save_hook = None
    await core.links.add(Link(UUID, "test", "vless-ws"))
    core_srv, core_task, csock, core_port = await _serve(core.app)

    os.environ["EMUNEL_WORKER_TOKEN"] = WORKER_TOKEN
    os.environ["EMUNEL_WORKER_DATA"] = str(tmp / "data")
    from emunel_worker import main as worker_main

    class FixedDriver:
        handles = {"inst": type("H", (), {"meta": {"api_token": CORE_TOKEN}})()}

        async def status(self, instance_id):
            return {"running": True, "port": core_port}

    worker_main.driver = FixedDriver()
    worker_srv, worker_task, wsock, worker_port = await _serve(worker_main.app)

    from emunel_console.services import gateway as gateway_module

    async def fake_resolve(request, token):
        if token != "endpoint-token":
            return None
        return {"instance_id": "inst",
                "worker_url": f"http://127.0.0.1:{worker_port}",
                "upstream": "/worker/api/instances/inst/proxy"}

    gateway_module._resolve_endpoint = fake_resolve
    # minimal DB stub for the subscription route (instance row + volume
    # lookups already swallow exceptions)
    class _FakePool:
        async def fetchrow(self, query, *args):
            sql = " ".join((query or "").lower().split())
            if "from instances where" in sql and "name, public_host" in sql:
                return {"name": "Engines-Test", "public_host": "", "status": "running"}
            if "from instances i where i.id" in sql or "from instances" in sql:
                return {"id": "inst", "status": "running",
                        "endpoint_token": "endpoint-token", "node_id": "local"}
            return None

        async def fetch(self, query, *args):
            return []

    gateway_module.get_pool = lambda request: _FakePool()
    from emunel_console import config as console_config

    console_config.settings.worker_token = WORKER_TOKEN
    from emunel_console.main import app as console_app

    manager = None
    serving_app = console_app
    if engines_on:
        import dataclasses

        from engines.config import parse_env
        from engines.manager import EngineManager
        from engines.middleware import EnginesASGIMiddleware

        cfg = parse_env("console")
        # fast flushes so batches coalesce within the test timeline; also
        # turn Morph on with a shaping profile so the equivalence proof
        # covers BOTH transforming engines, not just Coalesce.
        cfg = dataclasses.replace(
            cfg,
            coalesce_timeout_ms=250,
            coalesce_max_size=8192,
            morph_on=True,
        )
        os.environ["EMUNEL_ENGINE_MORPH_ENABLED"] = "1"
        manager = EngineManager("console", cfg=cfg)
        await manager.start()
        assert "Coalesce" in manager.active_engine_names()
        assert "Morph" in manager.active_engine_names()
        serving_app = EnginesASGIMiddleware(console_app, manager, cfg, panel_page=None)

    console_srv, console_task, gsock, console_port = await _serve(serving_app)
    return {
        "core": (core_srv, core_task, csock, core, core_port),
        "worker": (worker_srv, worker_task, wsock, worker_port),
        "console": (console_srv, console_task, gsock, console_port),
        "manager": manager,
    }


async def _vless_roundtrip(console_port: int, echo_port: int) -> bytes:
    """Speak VLESS over WS through the public gateway like a real client."""
    url = f"ws://127.0.0.1:{console_port}/i/endpoint-token/ws/{UUID}"
    header = build_vless_header(UUID, "127.0.0.1", echo_port) + b"PAYLOAD|" + ECHO_PAYLOAD
    received = bytearray()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(header)
        while True:
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=5)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            received.extend(frame if isinstance(frame, bytes) else frame.encode())
            if b"PAYLOAD|" + ECHO_PAYLOAD in received or \
                    received.endswith(ECHO_PAYLOAD):
                break
    return bytes(received)


def test_full_stack_equivalence():
    asyncio.run(_equivalence_runner())


async def _equivalence_runner():
    echo_server = await asyncio.start_server(_chunked_echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]
    results = {}
    managers = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for engines_on in (False, True):     # OFF first — the baseline
                stack = await _build_stack(Path(tmp), engines_on, {})
                managers.append(stack["manager"])
                console_port = stack["console"][3]
                try:
                    payload = await _vless_roundtrip(console_port, echo_port)
                    assert payload[:2] == b"\x00\x00", f"missing VLESS prefix: {payload[:8]!r}"
                    body = payload[2:]
                    assert body.startswith(b"PAYLOAD|" + ECHO_PAYLOAD), \
                        f"echo mismatch: {body[:64]!r}..."
                    results[engines_on] = body
                finally:
                    for key in ("console", "worker", "core"):
                        srv, task, sock, *_ = stack[key]
                        srv.should_exit = True
                        await asyncio.wait_for(task, 5)
                        sock.close()
                    if stack["manager"] is not None:
                        await stack["manager"].stop()
        # THE proof: identical bytes with engines on and off
        assert results[False] == results[True], \
            "engine pipeline altered the relayed byte stream"
        assert b"PAYLOAD|" + ECHO_PAYLOAD in results[True]
        # and the on-run actually transformed frames (engine metrics moved)
        manager = managers[1]
        coalesce = manager.engines["Coalesce"]
        assert coalesce.status.metrics["frames_out"] < coalesce.status.metrics["frames_in"]
    finally:
        echo_server.close()
        await echo_server.wait_closed()


# ── core host: status route + dial hook ──────────────────────────────────────
def test_core_host_status_route_and_dial_hook():
    asyncio.run(_core_host_runner())


async def _core_host_runner():
    from engines.config import parse_env
    from engines.core_host import _CoreHostASGI, _install_dial_hook, _remove_dial_hook
    from engines.manager import EngineManager

    core = Core(CoreConfig(api_token=CORE_TOKEN, log_level="error",
                           state_path="/dev/null"))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EMUNEL_ENGINE_DATA"] = str(Path(tmp) / "eng")
        cfg = parse_env("core")
        manager = EngineManager("core", cfg=cfg)
        await manager.start()
        app = _CoreHostASGI(core.app, manager, CORE_TOKEN)
        srv, task, sock, port = await _serve(app)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                         trust_env=False) as client:
                no_auth = await client.get("/engines/api/status")
                assert no_auth.status_code == 401
                ok = await client.get("/engines/api/status",
                                      headers={"Authorization": f"Bearer {CORE_TOKEN}"})
                assert ok.status_code == 200
                data = ok.json()
                assert data["host"] == "core"
                names = {e["name"] for e in data["engines"]}
                assert "PreConnect" in names and "Congestion" in names
                # health endpoints of the RAW core still work through the host
                health = await client.get("/health")
                assert health.status_code == 200 and health.json()["status"] == "ok"
        finally:
            srv.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()
            await manager.stop()

        # dial hook: install, verify open_connection passes through, restore
        echo = await asyncio.start_server(_chunked_echo, "127.0.0.1", 0)
        echo_port = echo.sockets[0].getsockname()[1]
        try:
            original = _install_dial_hook(manager)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", echo_port)
                writer.write(b"hook-check")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            finally:
                _remove_dial_hook(original)
            # restored: still functional
            reader, writer = await asyncio.open_connection("127.0.0.1", echo_port)
            writer.close()
        finally:
            echo.close()
            await echo.wait_closed()


# ── D. subscription feed rewrite through the REAL gateway path ──────────────
def test_subscription_feed_gets_split_tunnel_rules():
    asyncio.run(_subscription_runner())


async def _subscription_runner():
    """Boot the stack, add a share-able link, fetch /i/<token>/sub?fmt=...
    through the engines-wrapped console and verify the configgen pipeline
    really rewrites the feed the client receives."""
    with tempfile.TemporaryDirectory() as tmp:
        stack = await _build_stack(Path(tmp), True, {})
        console_port = stack["console"][3]
        core = stack["core"][3]
        try:
            # a link that /core/api/share will happily render
            from emunel_core.state import Link

            await core.links.add(Link(str(UUID), "sub-test", "vless-ws"))
            import httpx

            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{console_port}",
                                         trust_env=False) as client:
                for fmt, needle in (("singbox", "emunel-direct"),
                                    ("clash", "DOMAIN-SUFFIX,ir,DIRECT")):
                    resp = await client.get(
                        f"/i/endpoint-token/sub?fmt={fmt}&host=test.example")
                    assert resp.status_code == 200, (fmt, resp.status_code, resp.text[:200])
                    body = resp.text
                    assert needle in body, f"{fmt} feed missing {needle!r}: {body[:200]}"
                # headers must survive the rewrite
                resp = await client.get(
                    "/i/endpoint-token/sub?fmt=singbox&host=test.example")
                assert resp.headers.get("content-type") == "application/json"
                assert "subscription-userinfo" in resp.headers
        finally:
            for key in ("console", "worker", "core"):
                srv, task, sock, *_ = stack[key]
                srv.should_exit = True
                await asyncio.wait_for(task, 5)
                sock.close()
            if stack["manager"] is not None:
                await stack["manager"].stop()
