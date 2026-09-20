"""Live boot rehearsal for the Engines UI in the SERVED panel (Task 14 fix).

The previous engines task verified /api/engines but the Engine Settings
page lived only in the unserved modular frontend. This rehearsal boots the
real stack env-clean (Railway-style: only PORT) and proves the panel a
browser actually receives at "/" exposes the Engines section end-to-end.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

PORT = 3787
BASE = f"http://127.0.0.1:{PORT}"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(path: str, method: str = "GET", data: bytes | None = None,
         headers: dict | None = None):
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
    env.update({"PORT": str(PORT)})
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=REPO, env=env,
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
        if not check("env-clean boot healthy", healthy):
            print("\n".join(logs[-25:]))
            return 1

        # 1. build stamp surfaced by /health
        status, body, _ = http("/health")
        h = json.loads(body)
        check("/health carries a build stamp", bool(h.get("build")),
              f"build={h.get('build')}")

        # 2. the panel page a browser receives at "/" exposes the engines UI
        status, page, _ = http("/")
        page = page.decode("utf-8", "replace")
        check("served panel at / includes viewEngines JS", "viewEngines" in page)
        check("served panel includes Engines nav item", 'data-nav="engines"' in page)
        check("served panel nav routes to viewEngines",
              'name==="engines")viewEngines()' in page)
        check("served panel carries build stamp, not placeholder",
              "__EMUNEL_BUILD__" not in page and "build " in page)
        check("served panel keeps volume & time tab (regression)",
              "Volume & time" in page and "Traffic limits" in page)
        check("served panel keeps per-config quota UI (regression)",
              '"/links"' in page and "Add config" in page)
        check("no-store on panel shell",
              http("/")[2].get("cache-control") == "no-store")

        # 3. /api/engines guarded anon -> 401
        status, _, _ = http("/api/engines")
        check("/api/engines is guarded (401 anon)", status == 401, f"status={status}")

        # 4. admin login -> engine matrix
        login_data = json.dumps({"name": "admin", "password": "admin"}).encode()
        req = urllib.request.Request(BASE + "/auth/login-password", data=login_data,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
            # the session token itself is the CSRF secret (see auth.sessions)
            csrf = cookie.split("=", 1)[1] if "=" in cookie else ""
        check("admin/admin login works", bool(cookie))

        hdrs = {"Cookie": cookie, "X-EMUNEL-CSRF": csrf}
        status, body, _ = http("/api/engines", headers=hdrs)
        matrix = json.loads(body) if status == 200 else {}
        names = [e["name"] for e in matrix.get("engines", [])]
        check("engine matrix returned through the API",
              status == 200 and len(names) == 12, f"{len(names)} engines")

        # 5. hot disable -> enable via the API the page calls
        status, body, _ = http("/api/engines/Coalesce/disable", method="POST",
                              data=b"{}", headers=hdrs)
        d1 = json.loads(body) if status == 200 else {}
        coalesce = {e["name"]: e for e in (d1.get("status") or [])}.get("Coalesce", {})
        check("hot disable Coalesce via API", status == 200 and coalesce.get("active") is False,
              f"active={coalesce.get('active')}")
        status, body, _ = http("/api/engines/Coalesce/enable", method="POST",
                              data=b"{}", headers=hdrs)
        d2 = json.loads(body) if status == 200 else {}
        coalesce2 = {e["name"]: e for e in (d2.get("status") or [])}.get("Coalesce", {})
        check("hot re-enable Coalesce via API", status == 200 and coalesce2.get("active") is True,
              f"active={coalesce2.get('active')}")

        # 6. logs endpoint now admin-guarded
        status, _, _ = http("/api/engines/logs?name=Coalesce")
        check("engine logs endpoint guarded (401 anon)", status == 401, f"status={status}")
        status, body, _ = http("/api/engines/logs?name=Coalesce", headers=hdrs)
        check("engine logs endpoint works for admin", status == 200)

        failed = [n for n, ok, _ in CHECKS if not ok]
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
        if failed:
            print("FAILED:", ", ".join(failed))
            return 1
        return 0
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
