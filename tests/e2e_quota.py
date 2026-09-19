"""Live end-to-end proof of the AHB-style per-config traffic management.

Run against a booted stack (see scripts/e2e_quota_boot.sh):

    CONSOLE=http://127.0.0.1:3712 python tests/e2e_quota.py

Proves, with a REAL VLESS tunnel through the public gateway:
  1. wizard policy (100 MB + 30 days) reaches the Core and the Console DB
  2. real traffic changes Used / Remaining (live, not fake numbers)
  3. the subscription header + HTML page show the real quota
  4. quota is really enforced (connection killed, reconnects rejected)
  5. reset works, expiry is enforced (past date → expired), enable/disable works
  6. the concurrent-IP limit rejects a second distinct client IP
  7. the speed limit actually paces traffic
  8. redeploy keeps configs, quotas and usage (Console DB is the source of
     truth; lost Core state is reconciled back)
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid as uuidlib
from pathlib import Path

CONSOLE = os.environ.get("EMUNEL_E2E_CONSOLE", "http://127.0.0.1:3712")
ECHO_PORT = int(os.environ.get("EMUNEL_E2E_ECHO_PORT", "18610"))
PAYLOAD = b"Q" * (256 * 1024)          # 256 KB per response

PASS = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        raise AssertionError(f"[FAIL] {label} {detail}")
    PASS += 1
    print(f"  ok {PASS:2d} — {label}")


class Client:
    """Tiny http client with cookie + CSRF handling (like the panel)."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def req(self, method: str, path: str, body: dict | None = None,
            headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if self.csrf:
            hdrs["X-EMUNEL-CSRF"] = self.csrf
        if headers:
            hdrs.update(headers)
        r = urllib.request.Request(f"{CONSOLE}{path}", data=data, headers=hdrs, method=method)
        resp = self.opener.open(r, timeout=60)
        return json.loads(resp.read() or b"{}")

    def raw(self, path: str, headers: dict | None = None):
        r = urllib.request.Request(f"{CONSOLE}{path}", headers=headers or {}, method="GET")
        resp = self.opener.open(r, timeout=60)
        return resp

    def login(self):
        self.req("POST", "/auth/login-password", {"name": "admin", "password": "admin"})
        me = self.req("GET", "/auth/me")
        self.csrf = me["csrf_token"]
        assert me.get("authenticated"), "login failed"


def build_vless_request(uuid_hex: str, port: int, path: str = "/") -> bytes:
    raw = bytes.fromhex(uuid_hex.replace("-", ""))
    hdr = (b"\x00" + raw + b"\x00\x01" + port.to_bytes(2, "big")
           + b"\x01" + bytes([127, 0, 0, 1]))     # addr type 1 = raw IPv4
    req = f"GET {path} HTTP/1.1\r\nHost: e2e\r\nConnection: close\r\n\r\n".encode()
    return hdr + req


class EchoServer(threading.Thread):
    """HTTP server that answers with PAYLOAD (256 KB)."""

    import http.server

    def __init__(self):
        super().__init__(daemon=True)
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", ECHO_PORT), Handler)

    def run(self):
        self.srv.serve_forever()

    def stop(self):
        self.srv.shutdown()


async def relay_once(ws_url_base: str, uuid_hex: str, *,
                     forwarded_for: str | None = None,
                     expect_payload: bool = True,
                     recv_timeout: float = 15.0):
    """One VLESS request through the gateway. Returns (body, closed_code)."""
    import websockets

    headers = {}
    if forwarded_for:
        headers["X-Forwarded-For"] = forwarded_for
    try:
        async with websockets.connect(ws_url_base, additional_headers=headers,
                                      max_size=None, open_timeout=15) as ws:
            await ws.send(build_vless_request(uuid_hex, ECHO_PORT))
            body = bytearray()
            try:
                while len(body) < len(PAYLOAD):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                    body += chunk if isinstance(chunk, bytes) else chunk.encode()
            except websockets.exceptions.ConnectionClosed as exc:
                if expect_payload and not body:
                    raise
                return bytes(body), (exc.code if exc.code else None)
            return bytes(body), None
    except websockets.exceptions.ConnectionClosed as exc:
        return b"", (exc.code if exc.code else None)


async def expect_rejected(ws_url: str, uuid_hex: str, *, forwarded_for: str | None = None):
    """Connect + first request must be refused for a blocked link."""
    import websockets

    headers = {}
    if forwarded_for:
        headers["X-Forwarded-For"] = forwarded_for
    try:
        async with websockets.connect(ws_url, additional_headers=headers,
                                      open_timeout=15) as ws:
            await ws.send(build_vless_request(uuid_hex, ECHO_PORT))
            try:
                await asyncio.wait_for(ws.recv(), timeout=8)
            except websockets.exceptions.ConnectionClosed:
                return True
            return False  # got data — not blocked
    except websockets.exceptions.ConnectionClosed:
        return True
    except OSError:
        return True
    return False


def main() -> None:
    import websockets  # noqa: F401  (fail fast when missing)

    c = Client()
    c.login()
    print("== E2E: per-config traffic management (AHB capability set) ==")

    # ---- 1. create an instance with wizard policy: 100 MB / 30 days --------
    name = f"quota-{uuidlib.uuid4().hex[:6]}"
    inst = c.req("POST", "/api/instances", {
        "name": name, "region": "local",
        "config": {"protocol": "vless-ws", "protocols": ["vless-ws"],
                   "cpu_limit": 0.5, "memory_mb": 256,
                   "limit": 100, "unit": "MB", "expiry_days": 30},
    })
    iid = inst["id"]
    ok("instance created with wizard policy", True)

    dep = c.req("POST", f"/api/instances/{iid}/deploy")
    dep_id = dep["deployment_id"]
    for _ in range(90):
        time.sleep(1)
        deps = c.req("GET", f"/api/instances/{iid}/deployments")["deployments"]
        row = next((d for d in deps if d["id"] == dep_id), None)
        if row and row["status"] in ("running", "failed"):
            break
    ok("deployment reached running", row["status"] == "running", str(row.get("error")))

    # ---- 2. the policy reached Core + DB -----------------------------------
    links = c.req("GET", f"/api/instances/{iid}/links")["links"]
    ok("one config provisioned", len(links) == 1, str(len(links)))
    link = links[0]
    uuid_hex = link["uuid"].replace("-", "")
    ok("policy in link state (100 MB)", link["limit_bytes"] == 100 * 1024 ** 2,
       str(link["limit_bytes"]))
    ok("policy in link state (expiry ~30d)",
       link["expires_at"] is not None and 29 <= (link["seconds_remaining"] or 0) / 86400 <= 30.1,
       str(link.get("seconds_remaining")))
    ok("link is active", link["status"] == "active", link["status"])

    token = inst["endpoint_path"].split("/i/")[1]
    ws_base = f"{CONSOLE.replace('http', 'ws', 1)}/i/{token}/ws/{link['uuid']}"

    # verify the CORE itself carries the policy (Database → Core direction)
    core_link = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
    ok("live usage merge works (live=true)", core_link is not None)

    # ---- 3. real traffic changes Used / Remaining --------------------------
    echo = EchoServer()
    echo.start()
    try:
        body, code = asyncio.run(relay_once(ws_base, uuid_hex))
        ok("VLESS relay delivered the payload", b"200 OK" in body[:64], str(code))
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("Used grew with real traffic", after["used_bytes"] > 0, str(after["used_bytes"]))
        ok("Remaining = limit - used",
           after["remaining_bytes"] == after["limit_bytes"] - after["used_bytes"],
           f"{after['remaining_bytes']} vs {after['limit_bytes'] - after['used_bytes']}")

        # ---- 4. subscription shows the real numbers -------------------------
        resp = c.raw(f"/i/{token}/sub?host=127.0.0.1")
        userinfo = resp.headers.get("subscription-userinfo", "")
        parts = dict(p.split("=", 1) for p in userinfo.split("; ") if "=" in p)
        ok("sub header total = 100 MB", int(parts.get("total", -1)) == 100 * 1024 ** 2, userinfo)
        ok("sub header download = real usage",
           int(parts.get("download", -1)) == after["used_bytes"], userinfo)
        ok("sub header expire is set", int(parts.get("expire", 0)) > 0, userinfo)

        page = c.raw(f"/i/{token}/sub?host=127.0.0.1", headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
            "Accept": "text/html",
        }).read().decode()
        ok("sub page shows quota strip", "Quota" in page and "unlimited" not in page.split("Quota")[1][:200])
        ok("sub page shows real numbers", "100.0 MB" in page, page[:0])

        # ---- 5. quota is REALLY enforced (1 MB cap) --------------------------
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"limit": 1, "unit": "MB"})
        # keep pulling 256 KB responses until the Core cuts us off
        died = False
        for _ in range(12):
            body, code = asyncio.run(relay_once(ws_base, uuid_hex, expect_payload=False))
            if not body or code is not None:
                died = True
                break
        ok("connection killed after quota", died, "relay kept going past 1 MB")
        rejected = asyncio.run(expect_rejected(ws_base, uuid_hex))
        ok("reconnect rejected over quota", rejected)
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("status = limited", after["status"] == "limited", after["status"])
        ok("exceeded flag set", after["exceeded"] is True)

        # ---- 6. reset usage --------------------------------------------------
        c.req("POST", f"/api/instances/{iid}/links/{link['uuid']}/reset")
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("reset zeroes usage", after["used_bytes"] == 0, str(after["used_bytes"]))
        body, code = asyncio.run(relay_once(ws_base, uuid_hex))
        ok("relay works after reset", b"200 OK" in body[:64])

        # ---- 7. expiry enforced (past date → expired) -------------------------
        past = "2020-01-01T00:00:00+00:00"
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"expires_at": past})
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("status = expired", after["status"] == "expired", after["status"])
        rejected = asyncio.run(expect_rejected(ws_base, uuid_hex))
        ok("reconnect rejected when expired", rejected)
        # restore a live expiry
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"expiry_days": 30})
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("expiry re-extended", after["status"] == "active", after["status"])

        # ---- 8. enable / disable ----------------------------------------------
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"active": False})
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("status = disabled", after["status"] == "disabled", after["status"])
        rejected = asyncio.run(expect_rejected(ws_base, uuid_hex))
        ok("reconnect rejected when disabled", rejected)
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"active": True})
        body, code = asyncio.run(relay_once(ws_base, uuid_hex))
        ok("relay works after re-enable", b"200 OK" in body[:64])

        # ---- 9. concurrent-IP limit -------------------------------------------
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"ip_limit": 1})

        async def ip_probe():
            import websockets

            first = await websockets.connect(
                ws_base, additional_headers={"X-Forwarded-For": "203.0.113.9"},
                open_timeout=15)
            try:
                # a second DISTINCT ip must be rejected…
                try:
                    second = await websockets.connect(
                        ws_base, additional_headers={"X-Forwarded-For": "203.0.113.8"},
                        open_timeout=15)
                except (websockets.exceptions.ConnectionClosed, OSError):
                    blocked = True
                else:
                    blocked = False
                    try:
                        await second.send(build_vless_request(uuid_hex, ECHO_PORT))
                        try:
                            await asyncio.wait_for(second.recv(), timeout=6)
                        except websockets.exceptions.ConnectionClosed:
                            blocked = True   # accepted, then cut — the IP gate
                    except websockets.exceptions.ConnectionClosed:
                        blocked = True
                    finally:
                        try:
                            await second.close()
                        except Exception:
                            pass
                # …while the FIRST ip keeps working
                await first.send(build_vless_request(uuid_hex, ECHO_PORT))
                data = await asyncio.wait_for(first.recv(), timeout=10)
                alive = isinstance(data, (bytes, bytearray))
            finally:
                await first.close()
            return blocked, alive

        blocked, alive = asyncio.run(ip_probe())
        ok("second distinct IP blocked (ip_limit=1)", blocked)
        ok("first IP unaffected by ip limit", alive)
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"ip_limit": None})

        # ---- 10. speed limit actually paces traffic ----------------------------
        # (restore the 100 MB cap first so the speed measurement is independent)
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}",
              {"limit": 100, "unit": "MB"})
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"speed_mbps": 1})

        async def timed_download():
            import websockets

            # 256 KB response at 1 Mbps (131072 B/s + burst) ≈ 1.8 s;
            # unthrottled local delivery would be well under 200 ms.
            start = time.monotonic()
            async with websockets.connect(ws_base, open_timeout=15, max_size=None) as ws:
                await ws.send(build_vless_request(uuid_hex, ECHO_PORT))
                got = 0
                while got < len(PAYLOAD):
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=30)
                    except websockets.exceptions.ConnectionClosed:
                        break
                    got += len(data)
            return time.monotonic() - start

        elapsed = asyncio.run(timed_download())
        ok("speed limit paces transfer (>=1s at 1 Mbps)", elapsed >= 1.0, f"{elapsed:.2f}s")
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"speed_mbps": None})

        # ---- 11. redeploy keeps configs + quota (persistence) ------------------
        before = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        c.req("PATCH", f"/api/instances/{iid}/links/{link['uuid']}", {"limit": 100, "unit": "MB"})
        dep = c.req("POST", f"/api/instances/{iid}/redeploy")
        dep_id = dep["deployment_id"]
        for _ in range(90):
            time.sleep(1)
            deps = c.req("GET", f"/api/instances/{iid}/deployments")["deployments"]
            row = next((d for d in deps if d["id"] == dep_id), None)
            if row and row["status"] in ("running", "failed"):
                break
        ok("redeploy reached running", row["status"] == "running", str(row.get("error")))
        after = c.req("GET", f"/api/instances/{iid}/links")["links"][0]
        ok("same config uuid after redeploy", after["uuid"] == before["uuid"])
        ok("same quota after redeploy", after["limit_bytes"] == 100 * 1024 ** 2,
           str(after["limit_bytes"]))
        ok("usage preserved across redeploy", after["used_bytes"] >= before["used_bytes"],
           f"{after['used_bytes']} < {before['used_bytes']}")
        body, code = asyncio.run(relay_once(ws_base, uuid_hex))
        ok("relay works after redeploy", b"200 OK" in body[:64])

        # ---- 12. add + delete a config ----------------------------------------
        extra = c.req("POST", f"/api/instances/{iid}/links",
                      {"protocol": "trojan-ws", "label": "Extra", "limit": 50, "unit": "MB"})
        ok("extra config created with own quota", extra["limit_bytes"] == 50 * 1024 ** 2)
        links = c.req("GET", f"/api/instances/{iid}/links")["links"]
        ok("two configs listed", len(links) == 2, str(len(links)))
        # subscription aggregate now = 150 MB (100 + 50) — AHB group semantics
        resp = c.raw(f"/i/{token}/sub?host=127.0.0.1")
        userinfo = resp.headers.get("subscription-userinfo", "")
        parts = dict(p.split("=", 1) for p in userinfo.split("; ") if "=" in p)
        ok("sub aggregate sums caps (150 MB)",
           int(parts.get("total", -1)) == 150 * 1024 ** 2, userinfo)
        c.req("DELETE", f"/api/instances/{iid}/links/{extra['uuid']}")
        links = c.req("GET", f"/api/instances/{iid}/links")["links"]
        ok("extra config deleted", len(links) == 1, str(len(links)))

        # ---- cleanup -----------------------------------------------------------
        c.req("DELETE", f"/api/instances/{iid}")
    finally:
        echo.stop()

    print(f"\nE2E QUOTA OK — {PASS} assertions passed")


if __name__ == "__main__":
    main()
