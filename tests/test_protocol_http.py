"""Loopback transport smoke tests; no external client or public destinations."""
import asyncio
import hashlib
from pathlib import Path
import socket
import sys
import tempfile
from urllib.parse import urlsplit, parse_qs

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from emunel_core.app import Core
from emunel_core.config import CoreConfig
from emunel_core.links import generate_share_link
from emunel_core.state import Link

UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_trojan_xhttp_packet_and_stream_loopback():
    asyncio.run(_trojan_loopback())


async def _trojan_loopback():
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory() as tmp:
        core = Core(CoreConfig(state_path=str(Path(tmp) / "state.json"), log_level="error"))
        core._schedule_save = lambda: None
        core.ctx.save_hook = lambda: None
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(core.app, lifespan="off", log_level="error", timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
                for mode in ("packet-up", "stream-up"):
                    link = Link(UUID, "test", "trojan-xhttp-" + mode)
                    await core.links.add(link)
                    url = generate_share_link(link, "example.test")
                    query = parse_qs(urlsplit(url).query)
                    assert url.startswith("trojan://")
                    assert query["alpn"] == ["h2,http/1.1"]
                    path = query["path"][0] + "/session-" + mode
                    payload = b"trojan-xhttp-local-echo"
                    handshake = hashlib.sha224(UUID.encode()).hexdigest().encode() + b"\r\n\x01\x01\x7f\x00\x00\x01" + echo_port.to_bytes(2, "big") + b"\r\n" + payload
                    async with client.stream("GET", path) as down:
                        assert down.status_code == 200
                        response = await client.post(path + ("/0" if mode == "packet-up" else ""), content=handshake)
                        assert response.status_code == 200, response.text
                        received = b""
                        async for data in down.aiter_bytes():
                            received += data
                            if len(received) >= len(payload):
                                break
                        assert received == payload
                    await core.trojan_xhttp.teardown(UUID, "session-" + mode, "test complete")
        finally:
            for uuid, sid in list(core.trojan_xhttp.sessions):
                await core.trojan_xhttp.teardown(uuid, sid, "test shutdown")
            await core.vmess_runtime.close()
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()
            echo_server.close()
            await echo_server.wait_closed()


def test_vmess_management_missing_runtime():
    asyncio.run(_missing_runtime())


async def _missing_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        core = Core(CoreConfig(api_token="test-token", state_path=str(Path(tmp) / "state.json")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app), base_url="http://test") as client:
            result = await client.post("/core/api/links", headers={"Authorization": "Bearer test-token"},
                                       json={"protocol": "vmess-ws", "label": "test"})
            assert result.status_code == 503
            assert "EMUNEL_XRAY_BINARY" in result.json()["detail"]
            assert core.links.size() == 0
        await core.vmess_runtime.close()
