#!/usr/bin/env python3
"""Live boot rehearsal for the EMUNEL engines layer (production path).

Boots the REAL unified entrypoint (python main.py) on an injected PORT with
a clean environment, then verifies end-to-end:

  1. boot logs mention the engines layer + no crash
  2. /health stays 200 (platform-first never-crash contract)
  3. the panel page serves normally (UI untouched)
  4. the NEW /api/engines API exists and is admin-guarded (401 anon)
  5. admin password login -> /api/engines returns the full engine matrix
  6. hot toggle: disable + re-enable Coalesce through the API
  7. probe defense: a non-browser request to an unknown path gets an
     nginx-style 404 page (NOT the EMUNEL panel HTML)
  8. browser-like request to the same unknown path still gets the panel
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import urllib.request

PORT = 3779
BASE = f"http://127.0.0.1:{PORT}"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(path: str, method: str = "GET", data: bytes | None = None,
         headers: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(BASE + path, data=data, method=method,
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
        "EMUNEL_ENGINE_DATA": os.path.join(REPO, ".emunel-data", "rehearsal-engines"),
    })
    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    logs: list[str] = []
    try:
        deadline = time.time() + 60
        healthy = False
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                logs.append(line.rstrip())
                print("   |", line.rstrip()[:150])
            if proc.poll() is not None:
                break
            try:
                status, body, _ = http("/health")
                if status == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.4)
        print()
        ok = check("boot: /health 200 on injected PORT", healthy)
        if not healthy:
            return finish(proc, logs)

        status, body, headers = http("/health")
        check("health payload says EMUNEL Console", b"EMUNEL Console" in body)

        status, body, _ = http("/")
        check("panel page serves (UI untouched)", status == 200 and b"EMUNEL" in body)

        status, body, _ = http("/api/engines")
        check("/api/engines exists and is guarded (401 anon)", status == 401, f"status={status}")

        # admin login (bootstrap account; field name is "name")
        login = http("/auth/login-password", method="POST",
                     data=b'{"name":"admin","password":"admin"}',
                     headers={"Content-Type": "application/json"})
        check("admin/admin password login", login[0] == 200, f"status={login[0]}")
        if login[0] != 200:
            return finish(proc, logs)
        session = login[2].get("set-cookie", "").split(";")[0]
        csrf = session.split("=", 1)[1] if "=" in session else ""

        status, body, _ = http("/api/engines", headers={"Cookie": session})
        import json as _json

        try:
            matrix = _json.loads(body)
        except ValueError:
            matrix = {}
        names = {e["name"]: e for e in matrix.get("engines", [])}
        check("engine matrix returned (14 engines)", len(names) == 14,
              f"got {sorted(names)}")
        check("Coalesce active by default", names.get("Coalesce", {}).get("active") is True)
        check("Morph inactive with honest reason",
              names.get("Morph", {}).get("active") is False
              and "reason" in names.get("Morph", {}))
        check("FakeHandshake active by default",
              names.get("FakeHandshake", {}).get("active") is True)
        check("SplitTunnel active by default",
              names.get("SplitTunnel", {}).get("active") is True)
        check("data dir reported", bool(matrix.get("data_dir")))

        # hot toggle through the API (CSRF header = session token)
        auth_headers = {"Cookie": session, "X-EMUNEL-CSRF": csrf,
                        "Content-Type": "application/json"}
        status, body, _ = http("/api/engines/Coalesce/disable", method="POST",
                               headers=auth_headers)
        check("hot disable Coalesce", status == 200 and b'"ok":true' in body.replace(b" ", b""),
              f"status={status} {body[:80]!r}")
        status, body, _ = http("/api/engines", headers={"Cookie": session})
        matrix2 = _json.loads(body)
        coalesce2 = {e["name"]: e for e in matrix2.get("engines", [])}.get("Coalesce", {})
        check("Coalesce now inactive", coalesce2.get("active") is False)
        status, body, _ = http("/api/engines/Coalesce/enable", method="POST",
                               headers=auth_headers)
        check("hot re-enable Coalesce", status == 200)

        # probe defense: non-browser UA on unknown path -> fake nginx page
        status, body, headers = http("/wp-admin/setup.php",
                                     headers={"User-Agent": "python-urllib/3.12"})
        fake_ok = (status == 404 and b"nginx" in body.lower()
                   and b"EMUNEL" not in body)
        check("probe gets stock nginx 404 (not the panel)", fake_ok,
              f"status={status} server={headers.get('server', '')[:40]}")

        # browser-like request on unknown path still gets the SPA panel
        status, body, _ = http("/some-unknown-page",
                               headers={"User-Agent": "Mozilla/5.0 (X11; Linux)",
                                       "Accept": "text/html"})
        check("browser still gets the panel SPA", status == 200 and b"EMUNEL" in body)

        # selftest endpoint
        status, body, _ = http("/api/engines/selftest", method="POST", headers=auth_headers)
        check("engines selftest all_ok", status == 200 and b'"all_ok":true' in body.replace(b" ", b""),
              f"status={status} {body[:120]!r}")

        return finish(proc, logs)
    finally:
        pass


def finish(proc: subprocess.Popen, logs: list[str]) -> int:
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
