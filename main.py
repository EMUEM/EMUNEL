"""EMUNEL — unified service entrypoint (fork-and-go deployment).

One deployable unit contains the whole platform:

    python main.py        # or:  uvicorn main:app

  * EMUNEL Console API + frontend on ``$PORT`` (default 8080) — the public
    endpoint on the platform's domain (WebSocket capable)
  * an embedded EMUNEL Worker on an internal loopback port
  * EMUNEL Core instances as isolated child processes (process driver, OS
    resource limits) on the same node

The module exposes ``app`` (the Console ASGI application), so builders that
detect ``uvicorn main:app`` (railpack et al.) work with zero configuration.

Zero-config defaults:
  * ``DATABASE_URL`` (provided by managed platforms) is
    used when ``EMUNEL_DATABASE_URL`` is not set
  * ``EMUNEL_SECRET_KEY`` auto-generates and persists to a 0600 file on
    first boot
  * the internal worker token auto-generates per boot (console and worker
    share one process tree; set ``EMUNEL_WORKER_TOKEN`` explicitly when
    running split deployments)

Explicitly required for login: ``EMUNEL_GITHUB_CLIENT_ID`` and
``EMUNEL_GITHUB_CLIENT_SECRET`` (plus ``EMUNEL_PUBLIC_URL`` matching the
platform domain so the OAuth callback resolves).
"""
from __future__ import annotations

import atexit
import os
import secrets
import signal
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_setup_done = False
_worker: subprocess.Popen | None = None


def _writable_dir(candidates: list[Path | None]) -> Path | None:
    for cand in candidates:
        if cand is None:
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-probe"
            probe.write_text("ok")
            probe.unlink()
            return cand
        except OSError:
            continue
    return None


def ensure_secret_key() -> str:
    """Return a usable session secret, persisting a generated one if needed."""
    val = os.environ.get("EMUNEL_SECRET_KEY", "").strip()
    if len(val) >= 32:
        return val
    base = _writable_dir([Path("/data"), ROOT / ".emunel-data"])
    if base is not None:
        key_file = base / ".emunel_secret_key"
        try:
            if key_file.exists():
                val = key_file.read_text(encoding="utf-8").strip()
            if len(val) < 32:
                val = secrets.token_urlsafe(48)
                key_file.write_text(val, encoding="utf-8")
            os.chmod(key_file, 0o600)
            print(f"[emunel] EMUNEL_SECRET_KEY not set — persisted generated key to {key_file}",
                  file=sys.stderr)
            return val
        except OSError:
            pass
    print("[emunel] WARNING: EMUNEL_SECRET_KEY not set and not persistable — "
          "using an ephemeral key (sessions reset on restart)", file=sys.stderr)
    return secrets.token_urlsafe(48)


def pick_free_port(start: int, end: int | None = None) -> int:
    end = end if end is not None else start + 99
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free internal port in range {start}-{end}")


def _stop_worker() -> None:
    global _worker
    if _worker is not None and _worker.poll() is None:
        _worker.terminate()
        try:
            _worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _worker.kill()
    _worker = None


def _mask_dsn(dsn: str) -> str:
    """DSN with credentials masked, for safe boot logging."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(dsn)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        db = parts.path or ""
        return f"{host}{port}{db}"
    except ValueError:
        return "<unparseable dsn>"


def resolve_database_url() -> tuple[str, str] | None:
    """Find PostgreSQL config from platform-injected environment variables.

    Accepts every common injection style:
    * full URLs: EMUNEL_DATABASE_URL, DATABASE_URL, POSTGRES_URL,
      POSTGRESQL_URL, POSTGRES_CONNECTION_STRING
    * libpq variables: PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT and the
      POSTGRES_HOST/POSTGRES_USER/... variants many platforms inject
    Returns (dsn, source_name) or None when nothing is configured (the
    console then falls back to embedded SQLite — still fully functional).
    """
    from urllib.parse import quote

    for var in ("EMUNEL_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL",
                "POSTGRESQL_URL", "POSTGRES_CONNECTION_STRING"):
        val = os.environ.get(var, "").strip()
        if val:
            return val, var
    host = os.environ.get("PGHOST") or os.environ.get("POSTGRES_HOST")
    if host:
        user = os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER") or "postgres"
        password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or ""
        database = os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB") or "postgres"
        port = os.environ.get("PGPORT") or os.environ.get("POSTGRES_PORT") or "5432"
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        return f"postgresql://{auth}@{host}:{port}/{database}", "PGHOST/POSTGRES_* variables"
    return None


def setup() -> None:
    """Apply zero-config defaults and start the embedded worker (idempotent).

    Runs at import time so both ``python main.py`` and ``uvicorn main:app``
    boot the full platform.
    """
    global _setup_done, _worker
    if _setup_done:
        return
    _setup_done = True

    # ---- configuration defaults (zero-config friendly) --------------------
    port = int(os.environ.get("PORT", os.environ.get("EMUNEL_CONSOLE_PORT", "8080")))
    resolved = resolve_database_url()
    if resolved is None:
        # Zero-config: embedded SQLite inside the data dir. A PostgreSQL
        # database can be attached later by setting DATABASE_URL.
        base = _writable_dir([Path("/data"), ROOT / ".emunel-data"]) or Path("/tmp/emunel-data")
        os.environ["EMUNEL_DATABASE_URL"] = f"sqlite:///{base / 'emunel.db'}"
        print(f"[emunel] no PostgreSQL configured — using embedded SQLite at {base / 'emunel.db'}",
              file=sys.stderr)
        print("[emunel]   (attach a PostgreSQL database and set DATABASE_URL "
              "for production scale — data migrates via the admin export)", file=sys.stderr)
    else:
        os.environ["EMUNEL_DATABASE_URL"], db_source = resolved
        print(f"[emunel] PostgreSQL via {db_source} → {_mask_dsn(os.environ['EMUNEL_DATABASE_URL'])}",
              file=sys.stderr)
    os.environ["EMUNEL_SECRET_KEY"] = ensure_secret_key()
    os.environ.setdefault("EMUNEL_WORKER_TOKEN", secrets.token_urlsafe(32))
    os.environ.setdefault("EMUNEL_PUBLIC_URL", f"http://127.0.0.1:{port}")
    os.environ.setdefault("EMUNEL_NODE_ID", "local")
    os.environ.setdefault("EMUNEL_NODE_REGION", "local")

    data_root = _writable_dir([
        Path(os.environ["EMUNEL_WORKER_DATA"]) if os.environ.get("EMUNEL_WORKER_DATA") else None,
        Path("/data/instances"),
        ROOT / ".emunel-data" / "instances",
    ]) or Path("/tmp/emunel-instances")
    os.environ["EMUNEL_WORKER_DATA"] = str(data_root)

    worker_port = pick_free_port(9100)
    os.environ["EMUNEL_LOCAL_WORKER_URL"] = f"http://127.0.0.1:{worker_port}"

    # Core runs with the same interpreter/venv (single root requirements.txt).
    os.environ.setdefault("EMUNEL_CORE_PYTHON", sys.executable)
    os.environ.setdefault("EMUNEL_CORE_CWD", str(ROOT / "core"))

    # ── EMUNEL Engines: core-launch module decision ─────────────────────
    # Must happen BEFORE the worker subprocess below inherits the
    # environment. engines.host.wrap_console sets the same variable, but it
    # runs after this function returns — too late for a worker that already
    # copied the old environment. Without this, core-host engines
    # (PreConnect / Congestion / Compress / FEC) could never actually run in
    # instances on the unified single-service deployment. Best effort: any
    # failure keeps the previous behaviour (raw Core, engines off).
    try:
        sys.path.insert(0, str(ROOT))
        from engines.config import core_host_module

        _core_module = core_host_module()
        if _core_module:
            os.environ["EMUNEL_CORE_MODULE"] = _core_module
            os.environ.setdefault("EMUNEL_ENGINES_ROOT", str(ROOT))
    except Exception as _core_host_exc:
        print(f"[emunel] note: engines core-host pre-check skipped "
              f"({_core_host_exc}) — Cores launch without engines", file=sys.stderr)

    # ---- embedded worker ---------------------------------------------------
    worker_env = os.environ.copy()
    worker_env["EMUNEL_WORKER_HOST"] = "127.0.0.1"
    worker_env["EMUNEL_WORKER_PORT"] = str(worker_port)
    worker_env["EMUNEL_CONSOLE_URL"] = f"http://127.0.0.1:{port}"  # heartbeat target
    _worker = subprocess.Popen(
        [sys.executable, "-m", "emunel_worker"],
        cwd=str(ROOT / "worker"),
        env=worker_env,
    )
    atexit.register(_stop_worker)

    def _terminate(_num, _frame):
        _stop_worker()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _terminate)

    # ---- console app on sys.path -------------------------------------------
    sys.path.insert(0, str(ROOT / "console" / "api"))
    print(f"[emunel] console : {os.environ['EMUNEL_PUBLIC_URL']} (public port {port})")
    print(f"[emunel] worker  : internal on 127.0.0.1:{worker_port} "
          f"(process driver, data={data_root})")
    if not (os.environ.get("EMUNEL_GITHUB_CLIENT_ID") and os.environ.get("EMUNEL_GITHUB_CLIENT_SECRET")):
        print("[emunel] note: set EMUNEL_GITHUB_CLIENT_ID / EMUNEL_GITHUB_CLIENT_SECRET "
              "(callback <public-url>/auth/callback) to enable login", file=sys.stderr)


setup()

# ── EMUNEL Engines (plugin layer) ───────────────────────────────────────────
# Wraps the console app with the engine pipeline WITHOUT touching any console
# or core file. Engines disabled (EMUNEL_ENGINES_ENABLED=0) or an empty
# pipeline => `app` stays the raw console app and behaviour is bit-for-bit
# identical to before. Any engine failure is bypassed at runtime (see
# engines/README.md). This is the single sanctioned wiring point.
from emunel_console.main import app as _raw_console_app  # noqa: E402

try:
    from engines.host import wrap_console

    app = wrap_console(_raw_console_app)
except Exception as _engines_exc:  # absolute fallback: platform first
    print(f"[emunel] engines could not attach ({_engines_exc}) — "
          "running without them", file=sys.stderr)
    app = _raw_console_app


def main() -> int:
    port = int(os.environ.get("PORT", os.environ.get("EMUNEL_CONSOLE_PORT", "8080")))
    import uvicorn

    # Global in-flight request ceiling (storm guard): above it uvicorn
    # answers 503 immediately instead of queueing handlers that pile up
    # memory. Default 512 is far above legitimate trial-plan traffic and
    # far below fd exhaustion. 0 disables the cap.
    _limit = int(os.environ.get("EMUNEL_LIMIT_CONCURRENCY", "512") or 0)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("EMUNEL_LOG_LEVEL", "info"),
        limit_concurrency=_limit if _limit > 0 else None,
    )
    _stop_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
