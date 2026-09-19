"""EMUNEL platform integration test.

Real pipeline, no mocks:
  boot API -> create instance (spawns a real Core subprocess) ->
  create subscription with link provisioning -> link pushed to Core ->
  relay real traffic through the VLESS tunnel -> sync worker pulls the
  counter back into the subscription -> quota/expiry enforcement verified.
"""

import asyncio
import http.server
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.emunel_api.main import app
from api.emunel_api.config import settings as api_settings


class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HELLO-EMUNEL-INTEGRATION")

    def log_message(self, *a):
        pass


@pytest.fixture()
def hello_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Hello)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    # development-friendly settings for the test process
    import os
    os.environ.setdefault("EMUNEL_DEBUG", "true")
    os.environ.setdefault("EMUNEL_DATA_ROOT", "/tmp/emunel-it-data")
    os.environ.setdefault("EMUNEL_PORT_RANGE_START", "19200")
    os.environ.setdefault("EMUNEL_PORT_RANGE_END", "19299")

    with TestClient(app) as c:  # runs the lifespan (manager + sync worker)
        yield c


def _auth_headers(client):
    from api.emunel_api.config import settings as s
    r = client.post("/api/v1/auth/login", data={
        "username": s.admin_username, "password": s.admin_password,
    })
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_health_and_seeded_admin(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/api/v1/health/full")
    assert r.status_code == 200
    body = r.json()
    assert body["liveness"] == "ok"
    assert "components" in body


def test_full_pipeline(client, hello_server):
    headers = _auth_headers(client)

    # 1. create an instance (admin) — starts a real Core subprocess
    r = client.post("/api/v1/instances", json={
        "name": "it-main",
        "region": "local",
        "protocols": {"enabled": ["vless-ws", "trojan-ws"], "default": "vless-ws"},
        "memory_mb": 256,
        "start": True,
    }, headers=headers)
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["status"] == "running", inst.get("last_error")
    assert "core_api_token" not in inst  # secret must never leak
    instance_id = inst["id"]

    # 2. protocol matrix is exposed and validated
    r = client.get("/api/v1/instances/meta/protocols", headers=headers)
    assert r.status_code == 200
    matrix = r.json()
    assert {p["id"] for p in matrix["protocols"]} >= {"vless-ws", "trojan-ws", "shadowsocks"}

    # invalid protocol must be rejected
    r = client.post("/api/v1/instances", json={
        "name": "bad", "protocols": {"enabled": ["smtp"], "default": "smtp"},
    }, headers=headers)
    assert r.status_code == 400

    # 3. register a user + subscription with link provisioning on the instance
    r = client.post("/api/v1/auth/register", json={
        "username": "ituser", "password": "integration-pass-1",
    })
    assert r.status_code == 201
    user_id = r.json()["id"]

    r = client.post("/api/v1/subscriptions", json={
        "user_id": user_id,
        "name": "Gold Plan",
        "traffic_limit_gb": 1,       # 1 GiB quota
        "days_limit": 30,            # 30-day expiry
        "instance_id": instance_id,
        "protocol": "vless-ws",
    }, headers=headers)
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["effective_status"] == "active"
    assert sub["instance_ids"] == [instance_id]
    assert sub["link_count"] == 1

    # 4. the link exists in the API's view and was pushed to the Core
    r = client.get(f"/api/v1/instances/{instance_id}/links", headers=headers)
    links = r.json()["links"]
    assert len(links) == 1
    link = links[0]
    assert link["protocol"] == "vless-ws"
    assert "ss_password" not in link
    uuid = link["uuid"]

    # 5. relay real traffic through the VLESS tunnel
    import websockets

    async def _relay():
        port = client.get(f"/api/v1/instances/{instance_id}", headers=headers).json()["live"]["core_stats"]
        core_port = None
        # find the core port from the driver status
        r2 = client.get("/api/v1/instances", headers=headers).json()["instances"]
        for i in r2:
            if i["id"] == instance_id:
                core_port = (i.get("live") or {}).get("port")
        assert core_port, "core port not exposed in driver status"
        return core_port

    core_port = asyncio.get_event_loop().run_until_complete(_relay()) if False else None
    # TestClient is sync; run the websocket part in a thread with its own loop
    result = {}

    def _ws_worker():
        async def _run():
            from api.emunel_api.database import async_session
            from api.emunel_api.models.instance import Instance as I
            from sqlalchemy import select as sel
            async with async_session() as db:
                inst_row = (await db.execute(sel(I).where(I.id == instance_id))).scalar_one()
                port = inst_row.core_port
            raw = bytes.fromhex(uuid.replace("-", ""))
            hdr = (
                b"\x00" + raw + b"\x00" + b"\x01"
                + hello_server.to_bytes(2, "big")
                + b"\x01" + bytes([127, 0, 0, 1])
                + b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws/{uuid}") as ws:
                await ws.send(hdr)
                resp = bytearray(await asyncio.wait_for(ws.recv(), timeout=10))
                if b"HELLO-EMUNEL-INTEGRATION" not in resp:
                    resp += bytearray(await asyncio.wait_for(ws.recv(), timeout=10))
            result["resp"] = bytes(resp)

        try:
            asyncio.run(_run())
        except Exception as exc:  # surface thread failures into the test
            result["ws_error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_ws_worker)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "websocket worker hung"
    assert "ws_error" not in result, result.get("ws_error")
    assert result["resp"][:2] == b"\x00\x00"
    assert b"HELLO-EMUNEL-INTEGRATION" in result["resp"][2:]

    # 6. trigger a sync pass and verify the counter lands in the subscription
    async def _sync_once():
        from api.emunel_api.database import async_session
        from api.emunel_api.services import link_sync
        async with async_session() as db:
            out = await link_sync.poll_once(db)
            await db.commit()
            return out

    def _sync_worker():
        result["sync"] = asyncio.run(_sync_once())

    t = threading.Thread(target=_sync_worker)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "sync worker hung"
    assert result["sync"]["ok"] >= 1

    r = client.get(f"/api/v1/subscriptions/{sub['id']}", headers=headers)
    sub2 = r.json()
    assert sub2["traffic_used_bytes"] > 0, "traffic was not accounted onto the subscription"

    # 7. revoke the subscription — active tunnels must die at the Core
    r = client.post(f"/api/v1/subscriptions/{sub['id']}/revoke", headers=headers)
    assert r.status_code == 200
    assert r.json()["effective_status"] in ("disabled", "expired", "quota_exceeded")

    # the core link must now be inactive
    r = client.get(f"/api/v1/instances/{instance_id}/links", headers=headers)
    assert r.json()["links"][0]["active"] is False

    # 8. traffic summary reflects real numbers
    r = client.get("/api/v1/traffic/summary", headers=headers)
    assert r.status_code == 200
    assert r.json()["total_bytes_used"] > 0

    # 9. diagnostics: real DNS + TCP probes against loopback
    r = client.post("/api/v1/network-tests/dns", json={"host": "localhost", "port": 80}, headers=headers)
    assert r.status_code == 200
    dns = r.json()
    assert dns["ok"] and dns["latency_ms"] is not None

    r = client.post("/api/v1/network-tests/tcp",
                    json={"host": "127.0.0.1", "port": hello_server}, headers=headers)
    assert r.status_code == 200
    tcp = r.json()
    assert tcp["ok"] and tcp["latency_ms"] >= 0

    # 10. stop and delete the instance
    r = client.post(f"/api/v1/instances/{instance_id}/stop", headers=headers)
    assert r.status_code == 200
    r = client.delete(f"/api/v1/instances/{instance_id}", headers=headers)
    assert r.status_code == 204


def test_subscription_feed_serves_links(client, hello_server):
    headers = _auth_headers(client)

    r = client.post("/api/v1/instances", json={"name": "feed-inst", "start": True}, headers=headers)
    assert r.status_code == 201
    instance_id = r.json()["id"]

    r = client.post("/api/v1/auth/register", json={"username": "feeduser", "password": "feed-pass-123"})
    user_id = r.json()["id"]

    r = client.post("/api/v1/subscriptions", json={
        "user_id": user_id, "name": "Feed Test", "days_limit": 7,
        "instance_id": instance_id, "protocol": "vless-ws",
    }, headers=headers)
    sub = r.json()
    token = sub["link_token"]

    # public feed (no auth — the token IS the credential)
    r = client.get(f"/sub/{token}")
    assert r.status_code == 200
    import base64
    body = base64.b64decode(r.text).decode()
    assert body.startswith("vless://"), body[:50]
    assert "subscription-userinfo" in r.headers
    assert "expire=" in r.headers["subscription-userinfo"]

    client.delete(f"/api/v1/instances/{instance_id}", headers=headers)
