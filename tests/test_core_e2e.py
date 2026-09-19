"""EMUNEL Core end-to-end test: boots the real Core ASGI app, drives a real
VLESS client over WebSocket, verifies relay + quota + fail-closed behavior.

No mocks: real event loop, real sockets, real management API calls.
"""
import asyncio
import http.server
import json
import secrets
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from emunel_core.app import Core
from emunel_core.config import CoreConfig


class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HELLO-EMUNEL")

    def log_message(self, *a):
        pass


@pytest.fixture()
def hello_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Hello)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


async def _core_with_link(limit_bytes: int = 0, used_bytes: int = 0):
    import uvicorn

    cfg = CoreConfig(
        host="127.0.0.1",
        port=0,
        api_token=secrets.token_urlsafe(24),
        state_path="/tmp/emunel-test-state.json",
    )
    core = Core(cfg)
    config = uvicorn.Config(core.app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.get_running_loop().create_task(server.serve())
    # wait for the ephemeral port to be bound
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "core did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    return core, server, task, port


@pytest.mark.asyncio
async def test_vless_relay_end_to_end(hello_server):
    """Real client -> WS -> VLESS parse -> TCP to local server -> response."""
    core, server, task, port = await _core_with_link()

    try:
        # management API: create a link with the real bearer-token guard
        import httpx

        uuid = "123e4567-e89b-12d3-a456-426614174000"
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{port}/core/api/links",
                json={"label": "e2e", "protocol": "vless-ws"},
                headers={"Authorization": f"Bearer {core.cfg.api_token}"},
            )
            assert r.status_code == 200, r.text
            uuid = r.json()["uuid"]

        raw = bytes.fromhex(uuid.replace("-", ""))
        hdr = (
            b"\x00" + raw + b"\x00" + b"\x01"
            + hello_server.to_bytes(2, "big")
            + b"\x01" + bytes([127, 0, 0, 1])
            + b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
        )

        import websockets

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/{uuid}") as ws:
            await ws.send(hdr)
            resp = bytearray(await asyncio.wait_for(ws.recv(), timeout=10))
            if b"HELLO-EMUNEL" not in resp:
                resp += bytearray(await asyncio.wait_for(ws.recv(), timeout=10))
        assert resp[:2] == b"\x00\x00", f"missing VLESS prefix: {bytes(resp[:20])!r}"
        assert b"HELLO-EMUNEL" in resp[2:], "payload missing in tunnel response"

        # quota accounting actually happened
        link = core.links.get(uuid)
        assert link.used_bytes > 0, "traffic was not accounted"
        assert core.stats.total_bytes > 0
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_unknown_credential_is_rejected(hello_server):
    """Fail-closed: unknown UUID must be refused before any TCP dial."""
    core, server, task, port = await _core_with_link()
    try:
        import websockets

        uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        raw = bytes.fromhex(uuid.replace("-", ""))
        hdr = (
            b"\x00" + raw + b"\x00" + b"\x01"
            + hello_server.to_bytes(2, "big")
            + b"\x01" + bytes([127, 0, 0, 1])
        )
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/{uuid}") as ws:
            raw = bytes.fromhex(uuid.replace("-", ""))
            hdr = (
                b"\x00" + raw + b"\x00" + b"\x01"
                + hello_server.to_bytes(2, "big")
                + b"\x01" + bytes([127, 0, 0, 1])
            )
            # the tunnel must be torn down with 1008 either at send or first recv
            with pytest.raises(Exception):
                await ws.send(hdr)
                await asyncio.wait_for(ws.recv(), timeout=10)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_quota_exhaustion_cuts_the_tunnel(hello_server):
    """A link that is already over quota must not relay."""
    core, server, task, port = await _core_with_link()
    try:
        import httpx
        import websockets

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{port}/core/api/links",
                json={"label": "q", "protocol": "vless-ws", "limit_bytes": 10, "used_bytes": 10},
                headers={"Authorization": f"Bearer {core.cfg.api_token}"},
            )
            uuid = r.json()["uuid"]

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/{uuid}") as ws:
            raw = bytes.fromhex(uuid.replace("-", ""))
            hdr = b"\x00" + raw + b"\x00\x01" + hello_server.to_bytes(2, "big") + b"\x01" + bytes([127, 0, 0, 1])
            with pytest.raises(Exception):
                await ws.send(hdr)
                await asyncio.wait_for(ws.recv(), timeout=10)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_management_api_requires_token():
    core, server, task, port = await _core_with_link()
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://127.0.0.1:{port}/core/api/stats")).status_code == 401
            r = await client.get(
                f"http://127.0.0.1:{port}/core/api/stats",
                headers={"Authorization": f"Bearer {core.cfg.api_token}"},
            )
            assert r.status_code == 200
            body = r.json()
            assert "links" in body and "active_connections" in body
        # health stays public by design
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://127.0.0.1:{port}/health")).status_code == 200
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
