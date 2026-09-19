"""Full-relay regression: xHTTP stream-up must stream through gateway+worker.

Production bug: the console gateway buffered POST bodies (``await
request.body()``) and the worker proxy did the same. xHTTP stream-up POSTs
are infinite upload streams, so the first chunk never reached Core and
stream-up configs never connected (packet-up kept working — finite bodies).

All three hops (console gateway, worker proxy, Core) run as REAL uvicorn
servers here — httpx's ASGITransport cannot be used: it buffers response
bodies until the ASGI app call completes, which an infinite downlink never
does, so it would make this test lie.
"""
import asyncio
import os
from pathlib import Path
import socket
import sys
import tempfile

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))

from emunel_core.app import Core
from emunel_core.config import CoreConfig
from emunel_core.state import Link
from emunel_core.relay.trojan import build_trojan_request

UUID = "123e4567-e89b-12d3-a456-426614174000"
WORKER_TOKEN = "worker-test-token"
CORE_TOKEN = "core-test-token"


def test_stream_up_streams_through_gateway_and_worker():
    asyncio.run(_stream_up_through_relay())


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


async def _stream_up_through_relay():
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
        core = Core(CoreConfig(api_token=CORE_TOKEN, state_path=str(Path(tmp) / "state.json"), log_level="error"))
        core._schedule_save = lambda: None
        core.ctx.save_hook = lambda: None
        await core.links.add(Link(UUID, "test", "trojan-xhttp-stream-up"))
        core_srv, core_task, csock, core_port = await _serve(core.app)

        os.environ["EMUNEL_WORKER_TOKEN"] = WORKER_TOKEN
        os.environ["EMUNEL_WORKER_DATA"] = str(Path(tmp) / "data")
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
        from emunel_console import config as console_config

        console_config.settings.worker_token = WORKER_TOKEN
        gw = core_srv  # placeholder to keep names tidy
        from emunel_console.main import app as console_app
        console_srv, console_task, gsock, console_port = await _serve(console_app)

        edge = f"http://127.0.0.1:{console_port}"
        session = "e2e-session"
        core_path = f"/txhttp-siz10/stream-up/{UUID}/{session}"
        edge_path = f"/i/endpoint-token{core_path}"

        async def upload():
            yield build_trojan_request(UUID, "127.0.0.1", echo_port, b"chunk-one|")
            await asyncio.sleep(3.0)  # upload stays OPEN — nothing is "finished"
            yield b"chunk-two"

        async def wait_echo(down):
            async for part in down.aiter_bytes():
                if b"chunk-one" in part:
                    return True
            return False

        try:
            async with httpx.AsyncClient(base_url=edge, trust_env=False,
                                         timeout=httpx.Timeout(connect=5, read=None, write=None, pool=None)) as client:
                async with client.stream("GET", edge_path) as down:
                    assert down.status_code == 200, down.status_code
                    read_task = asyncio.create_task(wait_echo(down))
                    post_task = asyncio.create_task(client.post(edge_path, content=upload()))
                    try:
                        echoed = await asyncio.wait_for(read_task, timeout=8)
                    except asyncio.TimeoutError:
                        post_task.cancel()
                        raise AssertionError(
                            "echo did not arrive while upload still open — request body is being buffered")
                    assert echoed, "downlink closed before echo"
                    if post_task.done() and post_task.exception():
                        raise AssertionError(f"POST failed: {post_task.exception()!r}")
                    post_task.cancel()
        finally:
            for uuid, sid in list(core.trojan_xhttp.sessions):
                await core.trojan_xhttp.teardown(uuid, sid, "test shutdown")
            await core.vmess_runtime.close()
            for srv, task, sock in ((core_srv, core_task, csock), (worker_srv, worker_task, wsock), (console_srv, console_task, gsock)):
                srv.should_exit = True
                await asyncio.wait_for(task, 5)
                sock.close()
            echo_server.close()
            await echo_server.wait_closed()
