"""WS reconnect-storm / OOM fixes — regression suite.

Covers the six stabilization tasks from the bug report:
  1. stream-up 404 storm -> stopped instances answer 503 + Retry-After
     (proxy clients get ~30 bytes, browsers keep the friendly page)
  2. WebSocket lifecycle -> relay tasks awaited on teardown, guard released
  3. connection cap -> close 1013 try-again-later, counters visible
  4. engine/UI separation -> repeated hard memory pressure sheds the engine
     layer, the panel survives, engines restart from the panel
  5. 30s memory monitor line (rss / ws_conns / asyncio_tasks / gc)
  6. bounded buffers -> shared per-port httpx clients in the worker,
     always-run aclose() streaming cleanup, backpressure timeout
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "console" / "api"))
sys.path.insert(0, str(ROOT / "worker"))

# worker main.py requires the shared token at import time
os.environ.setdefault("EMUNEL_WORKER_TOKEN", "test-worker-token")


# ═════════════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════════════
def _app():
    from fastapi import FastAPI

    from emunel_console.services import gateway

    app = FastAPI()
    app.include_router(gateway.router)
    return app


PROXY_HEADERS = {"user-agent": "v2rayNG/1.8.0", "accept": "*/*"}
BROWSER_HEADERS = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64)",
                   "accept": "text/html,application/xhtml+xml"}


def _fake_resolve_factory(stopped: dict | None = None):
    stopped = stopped or {}

    async def _resolve(request, token):
        return stopped.get(token)
    return _resolve


@pytest.fixture()
def gateway_mod(monkeypatch):
    from emunel_console.services import gateway

    gateway._ws_guard.cap = 60
    gateway._ws_guard.active = 0
    gateway._ws_guard.rejected = 0
    monkeypatch.setattr(
        gateway, "_resolve_endpoint",
        _fake_resolve_factory({"stopped-token": {"stopped": True}}))
    return gateway


# ═════════════════════════════════════════════════════════════════════════
# TASK 1 — 404 storm -> 503 + Retry-After for stopped instances
# ═════════════════════════════════════════════════════════════════════════
def test_unknown_endpoint_404_tiny_for_proxy_clients(gateway_mod):
    from starlette.testclient import TestClient

    with TestClient(_app()) as client:
        r = client.get("/i/nope/xhttp-siz10/stream-up/uuid/sess",
                       headers=PROXY_HEADERS)
        assert r.status_code == 404
        assert "<html" not in r.text.lower()
        assert len(r.content) < 100          # storm hits pay ~30 bytes
        r2 = client.post("/i/nope/xhttp-siz10/stream-up/uuid/sess",
                         headers=PROXY_HEADERS)
        assert r2.status_code == 404          # GET and POST behave the same
        assert len(r2.content) < 100


def test_unknown_endpoint_browser_keeps_friendly_page(gateway_mod):
    from starlette.testclient import TestClient

    with TestClient(_app()) as client:
        r = client.get("/i/nope/xhttp-siz10/stream-up/uuid/sess",
                       headers=BROWSER_HEADERS)
        assert r.status_code == 404
        assert "<html" in r.text.lower()      # the human-readable page


def test_stopped_instance_503_retry_after(gateway_mod):
    from starlette.testclient import TestClient

    with TestClient(_app()) as client:
        for method in ("get", "post"):
            r = getattr(client, method)(
                "/i/stopped-token/xhttp-siz10/stream-up/uuid/sess",
                headers=PROXY_HEADERS)
            assert r.status_code == 503, method
            assert r.headers.get("retry-after") == "5"
            assert len(r.content) < 100
        # the browser page keeps its tone
        r = client.get("/i/stopped-token/xhttp-siz10/stream-up/uuid/sess",
                       headers=BROWSER_HEADERS)
        assert r.status_code == 503
        assert "restarting" in r.text.lower()


def test_stopped_status_page_and_qr(gateway_mod):
    from starlette.testclient import TestClient

    with TestClient(_app()) as client:
        page = client.get("/i/stopped-token", headers=BROWSER_HEADERS)
        assert page.status_code == 503
        qr = client.post("/i/stopped-token/api/qr", headers=PROXY_HEADERS,
                         json={"text": "vless://x"})
        assert qr.status_code == 503
        unknown = client.post("/i/nope/api/qr", headers=PROXY_HEADERS,
                              json={"text": "vless://x"})
        assert unknown.status_code == 404


def test_running_instance_reaches_upstream(monkeypatch, gateway_mod):
    """A resolvable target flows past the status gates to the worker hop
    (unreachable here) -> 502, proving the request was actually proxied."""
    from starlette.testclient import TestClient

    async def _resolve(request, token):
        return {"instance_id": "i-1", "worker_url": "http://127.0.0.1:1",
                "upstream": "/worker/api/instances/i-1/proxy"}
    monkeypatch.setattr(gateway_mod, "_resolve_endpoint", _resolve)
    with TestClient(_app()) as client:
        r = client.get("/i/live/xhttp-siz10/packet-up/uuid/sess/0",
                       headers=PROXY_HEADERS)
        assert r.status_code == 502


# ═════════════════════════════════════════════════════════════════════════
# TASK 2 + 3 — WS lifecycle, close codes, connection cap
# ═════════════════════════════════════════════════════════════════════════
def test_ws_unknown_close_1008(gateway_mod):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    with TestClient(_app()) as client:
        with client.websocket_connect("/i/nope/ws/uuid") as ws:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_text()                 # surfaces the server close
        assert ei.value.code == 1008
    assert gateway_mod._ws_guard.active == 0   # guard released on close


def test_ws_stopped_close_1013(gateway_mod):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    with TestClient(_app()) as client:
        with client.websocket_connect("/i/stopped-token/ws/uuid") as ws:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_text()
        assert ei.value.code == 1013            # "try again later"
    assert gateway_mod._ws_guard.active == 0


def test_ws_connection_cap_1013(gateway_mod):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    gateway_mod._ws_guard.cap = 1
    gateway_mod._ws_guard.active = 1            # one relay already live
    with TestClient(_app()) as client:
        with client.websocket_connect("/i/live/ws/uuid") as ws:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_text()
        assert ei.value.code == 1013
        assert gateway_mod._ws_guard.rejected == 1
    assert gateway_mod._ws_guard.active == 1    # the fake one untouched


@pytest.mark.asyncio
async def test_ws_relay_end_to_end_and_cleanup(monkeypatch, gateway_mod):
    """A full relay through a fake upstream: echo both directions, then
    disconnect — the guard must be back to zero afterwards."""
    from starlette.testclient import TestClient

    async def _resolve(request, token):
        return {"instance_id": "i-1", "worker_url": "http://127.0.0.1:9",
                "upstream": "/w"}

    class _FakeUpstream:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)           # never any downlink

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake = _FakeUpstream()

    def _connect(*args, **kwargs):
        from websockets.asyncio.client import connect as _real_connect
        import inspect
        # every kwarg we pass must exist on the REAL connect() signature —
        # an unknown kwarg flows into loop.create_connection() and kills
        # the relay at runtime (the write_timeout regression)
        real = set(inspect.signature(_real_connect).parameters)
        unknown = set(kwargs) - real
        assert not unknown, f"unknown websockets.connect kwargs: {unknown}"
        assert kwargs.get("max_size") == gateway_mod._WS_MAX_FRAME_BYTES
        assert kwargs.get("max_queue") == 32
        assert kwargs.get("write_limit") == 1024 * 1024
        return fake

    monkeypatch.setattr(gateway_mod, "_resolve_endpoint", _resolve)
    monkeypatch.setattr(gateway_mod.websockets, "connect", _connect)
    with TestClient(_app()) as client:
        with client.websocket_connect("/i/live/ws/uuid") as ws:
            ws.send_text("ping-up")
            for _ in range(50):
                if fake.sent:
                    break
                await asyncio.sleep(0.02)
            joined = b"".join(s.encode() if isinstance(s, str) else s
                             for s in fake.sent)
            assert b"ping-up" in joined
    assert gateway_mod._ws_guard.active == 0


# ═════════════════════════════════════════════════════════════════════════
# TASK 5 — 30s monitor line (oom guard) + middleware ws counter
# ═════════════════════════════════════════════════════════════════════════
def test_memory_guard_monitor_line():
    from engines.oom_guard import MemoryGuard

    lines = []

    class _Log:
        def info(self, msg, *args):
            lines.append(msg % args if args else msg)

        def warning(self, msg, *args):
            pass

    guard = MemoryGuard(soft_mb=10, hard_mb=20, log=_Log())
    guard.on_stat(lambda: {"ws_conns": 7})
    guard._rss_mb = lambda: 5.0
    guard.check()
    guard._log_stats("ok")
    assert len(lines) == 1
    assert "rss=5MB" in lines[0]
    assert "ws_conns=7" in lines[0]
    assert "asyncio_tasks=" in lines[0]
    assert "gc=(" in lines[0]


@pytest.mark.asyncio
async def test_middleware_counts_ws_connections():
    from engines.middleware import EnginesASGIMiddleware, ws_active_count

    class _Manager:
        pipelines = {}

    seen = {}

    async def app(scope, receive, send):
        seen["during"] = ws_active_count()

    mw = EnginesASGIMiddleware(app, _Manager(), cfg=None)
    scope = {"type": "websocket", "path": "/i/tok/ws/x", "headers": []}
    await mw(scope, None, None)
    assert seen["during"] == 1
    assert ws_active_count() == 0              # released after the handler


@pytest.mark.asyncio
async def test_middleware_does_not_count_non_gateway_ws():
    from engines.middleware import EnginesASGIMiddleware, ws_active_count

    class _Manager:
        pipelines = {}

    async def app(scope, receive, send):
        assert ws_active_count() == 0

    mw = EnginesASGIMiddleware(app, _Manager(), cfg=None)
    await mw({"type": "websocket", "path": "/other", "headers": []}, None, None)


# ═════════════════════════════════════════════════════════════════════════
# TASK 4 — engine shed on repeated hard memory pressure, panel survives
# ═════════════════════════════════════════════════════════════════════════
def test_memory_guard_emergency_fires_once():
    from engines.oom_guard import MemoryGuard

    fired = []
    guard = MemoryGuard(soft_mb=100, hard_mb=120, emergency_after=2)
    guard.on_emergency(lambda: fired.append(1))

    guard._rss_mb = lambda: 50.0
    assert guard.check() == "ok"
    assert not fired

    guard._rss_mb = lambda: 130.0
    assert guard.check() == "hard"              # first consecutive hard
    assert not fired
    assert guard.check() == "hard"             # second -> emergency
    assert fired == [1]
    assert guard.stats["emergency_stops"] == 1
    assert guard.status()["engines_shed"] is True

    assert guard.check() == "hard"              # already shed: no re-fire
    assert fired == [1]


def test_memory_guard_soft_resets_streak():
    from engines.oom_guard import MemoryGuard

    fired = []
    guard = MemoryGuard(soft_mb=100, hard_mb=120, emergency_after=2)
    guard.on_emergency(lambda: fired.append(1))
    guard._rss_mb = lambda: 130.0
    guard.check()
    guard._rss_mb = lambda: 110.0               # soft clears the streak
    assert guard.check() == "soft"
    guard._rss_mb = lambda: 130.0
    guard.check()
    assert not fired                            # streak was 1, not 2


@pytest.mark.asyncio
async def test_manager_emergency_shed_then_panel_restart(monkeypatch, tmp_path):
    """The acceptance test: engines shed under pressure, the manager (the
    panel's engines layer) keeps working, and Engine Settings restarts them."""
    from engines.config import parse_env
    from engines.manager import EngineManager
    from engines.base import KIND_FRAMES

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    cfg = parse_env("console")
    mgr = EngineManager("console", cfg=cfg)
    await mgr.start()
    try:
        active = mgr.active_engine_names()
        assert active, "expected at least one default-on engine (Coalesce)"

        # simulate: two consecutive hard memory ticks
        mgr.mem_guard._rss_mb = lambda: 10 ** 9
        mgr.mem_guard.check()
        mgr.mem_guard.check()

        await asyncio.sleep(0.1)                # the shed task runs
        assert mgr.active_engine_names() == []
        assert mgr.pipelines[KIND_FRAMES] == []
        assert mgr.mem_guard.status()["engines_shed"] is True
        reasons = [e.status.reason for e in mgr.engines.values()
                   if e.status.reason]
        assert any("memory guard emergency stop" in r for r in reasons)

        # panel restart: re-enable one engine from "Engine Settings"
        ok, reason = await mgr.set_engine_enabled(active[0], True)
        assert ok, reason
        assert active[0] in mgr.active_engine_names()
    finally:
        try:
            await mgr.stop()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════
# TASK 6 — bounded buffers, shared clients, always-run cleanup
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_worker_shared_core_client():
    from emunel_worker import main as wm

    c1 = wm._core_client(8500)
    c2 = wm._core_client(8500)
    assert c1 is c2                            # shared, not per-request
    await c1.aclose()
    assert c1.is_closed
    c3 = wm._core_client(8500)                 # closed -> rebuilt
    assert c3 is not c1
    await c3.aclose()


@pytest.mark.asyncio
async def test_worker_stream_core_closes_on_full_consume():
    from emunel_worker import main as wm

    class _FakeResp:
        closed = False

        async def aiter_raw(self):
            for chunk in (b"aa", b"bb"):
                yield chunk

        async def aclose(self):
            self.closed = True

    resp = _FakeResp()
    chunks = [c async for c in wm._stream_core(resp)]
    assert chunks == [b"aa", b"bb"]
    assert resp.closed is True


@pytest.mark.asyncio
async def test_worker_stream_core_closes_on_abort():
    """Client disconnects mid-stream: the generator's finally must still
    aclose() the upstream response (the original leak)."""
    from emunel_worker import main as wm

    class _FakeResp:
        closed = False

        async def aiter_raw(self):
            for chunk in (b"aa", b"bb", b"cc"):
                yield chunk

        async def aclose(self):
            self.closed = True

    resp = _FakeResp()
    gen = wm._stream_core(resp)
    assert await gen.__anext__() == b"aa"        # read one chunk...
    await gen.aclose()                           # ...then the client aborts
    assert resp.closed is True


def test_ws_guard_cap_semantics():
    from emunel_worker import main as wm

    guard = wm._ws_guard.__class__(cap=2)
    assert guard.acquire() and guard.acquire()
    assert guard.active == 2
    assert not guard.acquire()                   # over cap
    assert guard.rejected == 1
    guard.release()
    assert guard.active == 1
    assert guard.acquire()
    assert guard.active == 2
    guard.release()
    guard.release()
    guard.release()                              # clamped: never below zero
    assert guard.active == 0


@pytest.mark.asyncio
async def test_middleware_backpressure_timeout():
    """A stalled downlink (flushing + full buffer) drops the connection
    after the timeout instead of pinning the coroutine forever."""
    from engines.middleware import _WSConnection

    class _Manager:
        pipelines = {}

        async def dispatch_feedback(self, payload):
            pass

    class _Cfg:
        coalesce_max_buffer = 1024
        backpressure_timeout_s = 0.05
        morph_success_bytes = 4096

    conn = _WSConnection(_Manager(), _Cfg(), send=None,
                         scope={"path": "/i/x", "headers": []})
    conn._flushing = True                        # a flush is stuck...
    conn.pending = 1024                          # ...and the buffer is full
    conn.buffer = [(b"x" * 1024, False)]
    with pytest.raises(ConnectionError):
        await conn._add(b"y", False)
    assert conn._closed is True


@pytest.mark.asyncio
async def test_middleware_zero_frame_feedback_skipped():
    """Never-relayed connections (storm churn) do not spam engine feedback."""
    from engines.middleware import _WSConnection

    captured = []

    class _Manager:
        pipelines = {}

        async def dispatch_feedback(self, payload):
            captured.append(payload)

    class _Cfg:
        coalesce_max_buffer = 1024
        backpressure_timeout_s = 15.0
        morph_success_bytes = 4096

    churned = _WSConnection(_Manager(), _Cfg(), send=None,
                            scope={"path": "/i/x", "headers": []})
    await churned.close()                        # up=down=0 -> no feedback
    assert captured == []

    real = _WSConnection(_Manager(), _Cfg(), send=None,
                         scope={"path": "/i/x", "headers": []})
    real.up_bytes = 100
    real.down_bytes = 4096
    await real.close()
    assert len(captured) == 1
    assert captured[0]["down_bytes"] == 4096


def test_env_knobs_documented():
    """The new knobs exist with sane defaults."""
    from engines.config import parse_env

    cfg = parse_env("console")
    assert cfg.backpressure_timeout_s == 15.0
    assert cfg.mem_emergency_stops == 2
