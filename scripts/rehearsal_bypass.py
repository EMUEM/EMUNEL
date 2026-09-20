#!/usr/bin/env python3
"""Live rehearsal: quota hardening + Bypass engines (production path).

Boots the REAL unified entrypoint (python main.py) on an injected PORT with
a clean environment, then verifies end-to-end:

  A. QUOTA HARDENING — the AHB-style bypass is closed:
     1. instance created WITHOUT any per-config limit (per-link unlimited —
        the exact setup that previously let traffic flow unbounded)
     2. volume limit set via the panel API (small, e.g. 3 MB)
     3. the enforcement LOOP is deliberately disabled
        (EMUNEL_VOLUME_CHECK_SECONDS=9999) — so the ONLY thing that can cut
        traffic is the Core's relay-time cap
     4. REAL VLESS tunnel through the public gateway: payload relays until
        the cap, the connection is cut mid-stream, reconnects are refused
     5. the panel volume state shows the real used bytes

  B. BYPASS ENGINES — SNI Spoofing + REALITY:
     6. engine matrix includes SNISpoof + Reality (14 engines total)
     7. /api/engines/sni/status profile; config update persists; the
        server-side fragment-plan test returns ok
     8. the client helper script downloads
     9. REALITY: honest "runtime not configured" without a pinned Xray,
        keypair generates + persists, RAW/XHTTP/gRPC configs generate with
        a valid vless:// reality link
    10. the served panel page carries the Bypass tab (nav + viewBypass)
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid as uuidlib

PORT = 3781
CONSOLE = f"http://127.0.0.1:{PORT}"
ECHO_PORT = 18612
PAYLOAD = b"Q" * (256 * 1024)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if self.csrf:
            hdrs["X-EMUNEL-CSRF"] = self.csrf
        if headers:
            hdrs.update(headers)
        r = urllib.request.Request(f"{CONSOLE}{path}", data=data, headers=hdrs,
                                   method=method)
        resp = self.opener.open(r, timeout=60)
        return json.loads(resp.read() or b"{}")

    def text(self, path, headers=None):
        r = urllib.request.Request(f"{CONSOLE}{path}", headers=headers or {},
                                   method="GET")
        resp = self.opener.open(r, timeout=30)
        return resp.status, resp.read()

    def login(self):
        self.req("POST", "/auth/login-password",
                 {"name": "admin", "password": "admin"})
        me = self.req("GET", "/auth/me")
        self.csrf = me["csrf_token"]
        assert me.get("authenticated"), "login failed"


class EchoServer(threading.Thread):
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


def build_vless_request(uuid_hex, port, path="/"):
    raw = bytes.fromhex(uuid_hex.replace("-", ""))
    hdr = (b"\x00" + raw + b"\x00\x01" + port.to_bytes(2, "big")
           + b"\x01" + bytes([127, 0, 0, 1]))
    req = f"GET {path} HTTP/1.1\r\nHost: e2e\r\nConnection: close\r\n\r\n".encode()
    return hdr + req


async def relay_once(ws_url, uuid_hex, forwarded_for=None, recv_timeout=15.0):
    """One VLESS request through the gateway. Returns (body, closed_code)."""
    import websockets

    headers = {}
    if forwarded_for:
        headers["X-Forwarded-For"] = forwarded_for
    try:
        async with websockets.connect(ws_url, additional_headers=headers,
                                      max_size=None, open_timeout=15) as ws:
            await ws.send(build_vless_request(uuid_hex, ECHO_PORT))
            body = bytearray()
            try:
                while len(body) < len(PAYLOAD):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                    body += chunk if isinstance(chunk, bytes) else chunk.encode()
            except websockets.exceptions.ConnectionClosed as exc:
                return bytes(body), (exc.code if exc.code else None)
            return bytes(body), None
    except websockets.exceptions.ConnectionClosed as exc:
        return b"", (exc.code if exc.code else None)


async def expect_rejected(ws_url, uuid_hex, forwarded_for=None):
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
            return False
    except (websockets.exceptions.ConnectionClosed, OSError):
        return True
    return False


def raw_http(path, method="GET", data=None, headers=None):
    req = urllib.request.Request(f"{CONSOLE}{path}", data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}


def main() -> int:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("EMUNEL_", "DATABASE_", "PG", "POSTGRES", "GITHUB_"))
           and k not in ("PORT",)}
    env.update({
        "PORT": str(PORT),
        "EMUNEL_ENGINES_ENABLED": "1",
        # the enforcement LOOP is the SECOND line; disable it for this
        # rehearsal so the cut can only come from the Core's relay-time cap
        "EMUNEL_VOLUME_CHECK_SECONDS": "9999",
        "EMUNEL_ENGINE_DATA": os.path.join(REPO, ".emunel-data", "rehearsal-bypass"),
    })
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=REPO, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    logs: list[str] = []
    try:
        deadline = time.time() + 60
        healthy = False
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                logs.append(line.rstrip())
            if proc.poll() is not None:
                break
            try:
                status, _, _ = raw_http("/health")
                if status == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.4)
        check("boot: /health 200", healthy)
        if not healthy:
            return finish(proc, logs)

        c = Client()
        c.login()
        check("admin/admin login", True)

        # ================= A. QUOTA HARDENING ==============================
        print("== A. quota hardening: relay-time cut (AHB bypass closed) ==")
        name = f"bypass-proof-{uuidlib.uuid4().hex[:6]}"
        inst = c.req("POST", "/api/instances", {
            "name": name, "region": "local",
            "config": {"protocol": "vless-ws", "protocols": ["vless-ws"],
                       "cpu_limit": 0.5, "memory_mb": 256},
        })          # NOTE: no per-config limit — links are unlimited
        iid = inst["id"]
        dep = c.req("POST", f"/api/instances/{iid}/deploy")
        dep_id = dep["deployment_id"]
        row = None
        for _ in range(90):
            time.sleep(1)
            deps = c.req("GET", f"/api/instances/{iid}/deployments")["deployments"]
            row = next((d for d in deps if d["id"] == dep_id), None)
            if row and row["status"] in ("running", "failed"):
                break
        check("instance deployed (per-config links unlimited)", row and row["status"] == "running",
              str(row and row.get("error")))

        links = c.req("GET", f"/api/instances/{iid}/links")["links"]
        check("one config provisioned, unlimited", len(links) == 1
              and links[0]["limit_bytes"] is None, str(links[0].get("limit_bytes")))
        uuid_hex = links[0]["uuid"].replace("-", "")

        cap = 3 * 1024 * 1024                      # 3 MB instance cap
        vol = c.req("PUT", f"/api/instances/{iid}/volume",
                    {"limit_bytes": cap})
        check(f"volume limit set to {cap} bytes via panel API",
              vol.get("limit_bytes") == cap, json.dumps(vol)[:120])
        state0 = c.req("GET", f"/api/instances/{iid}/volume")
        check("volume state live", state0.get("live") is True
              and state0.get("limit_bytes") == cap, json.dumps(state0)[:160])

        token = inst["endpoint_path"].split("/i/")[1]
        ws_base = f"{CONSOLE.replace('http', 'ws', 1)}/i/{token}/ws/{links[0]['uuid']}"

        echo = EchoServer()
        echo.start()
        relayed = 0
        cut_mid_stream = False
        refused = False
        try:
            # relay until the instance cap cuts the stream
            for i in range(40):
                body, code = asyncio.run(relay_once(ws_base, uuid_hex))
                relayed += len(body)
                if code is not None and not body:
                    refused = True
                    break
                if code is not None and len(body) < len(PAYLOAD):
                    cut_mid_stream = True
                    break
                if not body:
                    refused = True
                    break
            # after the cut, a NEW connection must be refused
            refused_after = asyncio.run(expect_rejected(ws_base, uuid_hex))
            check("reconnect after the cap is refused", refused_after is True)
        finally:
            echo.stop()

        overshoot = relayed - cap
        check("traffic really flowed (multiple payloads)", relayed >= 256 * 1024,
              f"{relayed} bytes relayed")
        check("cut happened at/just past the cap (bounded overshoot)",
              0 <= overshoot <= 3 * 1024 * 1024,
              f"relayed={relayed} cap={cap} overshoot={overshoot}")
        check("cut mid-stream or refused (quota enforcement fired)",
              cut_mid_stream or refused, f"cut={cut_mid_stream} refused={refused}")

        state1 = c.req("GET", f"/api/instances/{iid}/volume")
        check("panel shows used ~= relayed (relay-time accounting)",
              abs(state1.get("used_bytes", 0) - relayed) <= 512 * 1024,
              f"used={state1.get('used_bytes')} relayed={relayed}")
        check("instance still 'running' (loop disabled — cut came from the Core)",
              c.req("GET", f"/api/instances/{iid}")["status"] == "running")
        # clear the cap so the teardown is clean
        c.req("PUT", f"/api/instances/{iid}/volume", {"limit_bytes": 0})
        c.req("POST", f"/api/instances/{iid}/stop")

        # ================= B. BYPASS ENGINES ==============================
        print("== B. bypass engines: SNI + REALITY ==")
        matrix = c.req("GET", "/api/engines")
        names = {e["name"]: e for e in matrix.get("engines", [])}
        check("engine matrix has 14 engines (incl. SNISpoof + Reality)",
              len(names) == 14, f"got {len(names)}: {sorted(names)}")
        check("SNISpoof active (generator)", names.get("SNISpoof", {}).get("active") is True,
              str(names.get("SNISpoof", {}).get("reason"))[:80])
        check("Reality active (generator)", names.get("Reality", {}).get("active") is True,
              str(names.get("Reality", {}).get("reason"))[:80])

        # anonymous access must be refused
        status, body, _ = raw_http("/api/engines/sni/status")
        check("/api/engines/sni/status guarded (401 anon)", status == 401, f"status={status}")
        status, body, _ = raw_http("/api/engines/reality/status")
        check("/api/engines/reality/status guarded (401 anon)", status == 401, f"status={status}")

        sni = c.req("GET", "/api/engines/sni/status")
        check("SNI profile returned", sni.get("profile", {}).get("method") == "combined",
              json.dumps(sni.get("profile", {}))[:140])
        upd = c.req("POST", "/api/engines/sni/config",
                    {"strategy": "multi", "delay": 0.2, "ttl_value": 3})
        check("SNI profile update accepted", upd.get("ok") is True
              and upd["profile"]["fragment_strategy"] == "multi")
        back = c.req("GET", "/api/engines/sni/status")
        check("SNI profile persisted", back["profile"]["fragment_strategy"] == "multi"
              and back["profile"]["ttl_value"] == 3)
        test = c.req("POST", "/api/engines/sni/test")
        check("SNI plan test ok (multi fragments)", test.get("ok") is True
              and len(test.get("fragment_sizes", [])) > 3,
              json.dumps(test)[:140])
        test2 = c.req("POST", "/api/engines/sni/config", {"strategy": "sni_split"})
        test2 = c.req("POST", "/api/engines/sni/test")
        check("SNI plan test ok (sni_split cuts inside SNI)",
              test2.get("ok") is True and test2.get("cut_inside_sni") is True,
              json.dumps(test2)[:160])
        bad = None
        try:
            c.req("POST", "/api/engines/sni/config", {"method": "nonsense"})
        except urllib.error.HTTPError as exc:
            bad = exc.code
        check("SNI config rejects nonsense (400)", bad == 400, f"status={bad}")

        status, text = c.text("/api/engines/sni/helper?download=1")
        check("helper script downloads", status == 200 and b"BypassProxy" in text
              and b"--connect" in text, f"status={status} len={len(text)}")

        rea = c.req("GET", "/api/engines/reality/status")
        check("REALITY honest runtime status (not configured)",
              rea.get("runtime", {}).get("configured") is False
              and rea.get("runtime", {}).get("running") is False
              and bool(rea.get("runtime", {}).get("how_to_enable")),
              json.dumps(rea.get("runtime", {}))[:140])
        check("REALITY keypair auto-generated for status",
              bool(rea.get("public_key")) and len(rea["public_key"]) == 44)
        keys = c.req("POST", "/api/engines/reality/keys")
        check("REALITY keypair generation", len(keys.get("private_key", "")) == 44
              and len(keys.get("public_key", "")) == 44
              and bool(keys.get("client_uuid")))
        gen = c.req("POST", "/api/engines/reality/generate", {"transport": "raw"})
        link = gen.get("share_url", "")
        check("REALITY raw config generated",
              link.startswith("vless://") and "security=reality" in link
              and "pbk=" in link and "sid=" in link and "sni=blubank.com" in link,
              link[:120])
        check("REALITY inbound carries privateKey + serverNames",
              gen["inbound"]["streamSettings"]["realitySettings"]["privateKey"]
              and gen["inbound"]["streamSettings"]["realitySettings"]["serverNames"])
        gen2 = c.req("POST", "/api/engines/reality/generate", {"transport": "xhttp"})
        check("REALITY xhttp variant", gen2["share_url"].startswith("vless://")
              and "type=xhttp" in gen2["share_url"], gen2["share_url"][:100])
        gen3 = c.req("POST", "/api/engines/reality/generate", {"transport": "grpc"})
        check("REALITY grpc variant", "serviceName=" in gen3["share_url"],
              gen3["share_url"][:100])

        # panel wiring in the SERVED page
        status, body, _ = raw_http("/")
        check("served panel has the Bypass tab",
              b'data-nav="bypass"' in body and b"viewBypass" in body)

        # engines-off: kill the layer and confirm the panel still boots
        # (handled by unit tests + previous rehearsal; here we only assert
        # the page rendered above with zero JS errors in the browser E2E)

        return finish(proc, logs)
    finally:
        pass


def finish(proc, logs):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    failed = [name for name, ok, _ in CHECKS if not ok]
    print()
    print(f"RESULT: {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        print("--- last 25 log lines ---")
        for line in logs[-25:]:
            print("   |", line[:160])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
