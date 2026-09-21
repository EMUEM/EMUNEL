"""Bypass fixes — real HTTP compression, REALITY reachability probe, panel
config-tab controls (rename + set-count).

Covers the three user-reported issues of this session:
  * "Engine compression هم کار نمیکند"  -> the Compress engine now has a real
    working channel (panel/API/subscription HTTP responses) with measurable
    savings; hot-enable via Engine Settings activates it.
  * "REALITY configs don't ping"        -> generate() now really probes the
    target address:port and labels unreachable configs honestly; the
    baked-Xray path (Dockerfile ARG XRAY_VERSION) is recognized.
  * rename/count controls               -> the served panel ships the Name
    field in the edit drawer + the set-count control (existing link APIs).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import socket
import threading
from pathlib import Path

import pytest

from engines.bus import EventBus
from engines.config import parse_env
from engines.engines.reality import RealityEngine, baked_xray
from engines.state import EngineStateStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    return parse_env("test")


@pytest.fixture()
def store(tmp_path):
    return EngineStateStore(str(tmp_path / "engines-store"))


def make_reality(env, store):
    engine = RealityEngine(env, EventBus(), store)
    asyncio.run(engine.init({}))
    return engine


# ── HTTP response compression (the Compress engine's real channel) ──────────
def _make_app(*, env_on=False, engine_active=False, env=None, store=None):
    from fastapi import FastAPI

    from engines.http_compress import HTTPCompressMiddleware

    app = FastAPI()

    @app.get("/page")
    async def page():
        return {"ok": True, "data": "x" * 4096}       # compressible JSON

    @app.get("/binary")
    async def binary():
        from fastapi import Response

        return Response(content=b"\x00\x01" * 4096,
                         media_type="application/octet-stream")

    @app.get("/tiny")
    async def tiny():
        return {"ok": True}

    @app.get("/stream")
    async def stream():
        from starlette.responses import StreamingResponse

        async def gen():
            for i in range(8):
                yield f"chunk-{i}-" + "y" * 512
        return StreamingResponse(gen(), media_type="text/plain")

    class StubManager:
        engines = {}
        cfg = env

    if engine_active:
        from engines.engines.compression import CompressionEngine

        engine = CompressionEngine(env or parse_env("console"),
                                   EventBus(), store or EngineStateStore("/tmp"))
        engine.status.active = True
        StubManager.engines["Compress"] = engine

    cfg = env
    if env_on and cfg is not None:
        import dataclasses

        cfg = dataclasses.replace(cfg, http_compress_on=True)
    elif env_on:
        class Cfg:
            http_compress_on = True
        cfg = Cfg()
    return HTTPCompressMiddleware(app, StubManager() if cfg else None, cfg), app


def _drive(middleware, path, accept="gzip"):
    """Raw ASGI call — returns the send() messages so we can assert on the
    ACTUAL compressed bytes (httpx would transparently decompress them).
    receive() behaves like a real server: one request message, then blocks
    until cancelled (Starlette's disconnect listener expects that)."""
    messages = []
    served_request = False

    async def receive():
        nonlocal served_request
        if not served_request:
            served_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()          # block: no disconnect during test

    async def send(message):
        messages.append(message)

    headers = [(b"accept-encoding", accept.encode())] if accept else \
        [(b"user-agent", b"pytest")]
    scope = {"type": "http", "method": "GET", "path": path,
             "headers": headers, "query_string": b""}
    asyncio.run(middleware(scope, receive, send))
    return messages


def _body(messages):
    return b"".join(m.get("body", b"") or b"" for m in messages
                     if m["type"] == "http.response.body")


def _start(messages):
    for m in messages:
        if m["type"] == "http.response.start":
            return m
    return {}


def _header_of(message, name):
    for k, v in message.get("headers", []):
        if k.decode("latin-1").lower() == name:
            return v.decode("latin-1")
    return None


def test_http_compression_compresses_text_responses():
    middleware, _ = _make_app(env_on=True)
    messages = _drive(middleware, "/page")
    start = _start(messages)
    assert start.get("status") == 200
    assert _header_of(start, "content-encoding") == "gzip"
    assert _header_of(start, "vary") == "Accept-Encoding"
    raw = _body(messages)
    decompressed = gzip.decompress(raw)
    payload = json.loads(decompressed)
    assert payload["ok"] is True
    assert len(raw) < len(decompressed)                 # really saved bytes


def test_http_compression_skips_binary_and_tiny():
    middleware, _ = _make_app(env_on=True)
    messages = _drive(middleware, "/binary")
    start = _start(messages)
    assert _header_of(start, "content-encoding") is None
    assert len(_body(messages)) == 8192
    messages = _drive(middleware, "/tiny")
    start = _start(messages)
    assert _header_of(start, "content-encoding") is None   # below MIN_SIZE


def test_http_compression_streams_safely():
    middleware, _ = _make_app(env_on=True)
    messages = _drive(middleware, "/stream")
    start = _start(messages)
    assert _header_of(start, "content-encoding") == "gzip"
    text = gzip.decompress(_body(messages)).decode()
    assert text.startswith("chunk-0-") and text.endswith("y")


def test_http_compression_inactive_without_flag_or_engine():
    middleware, _ = _make_app(env_on=False, engine_active=False,
                              env=parse_env("test"),
                              store=EngineStateStore("/tmp/emunel-test-store"))
    messages = _drive(middleware, "/page")
    start = _start(messages)
    assert _header_of(start, "content-encoding") is None
    assert json.loads(_body(messages))["ok"] is True


def test_http_compression_activates_through_hot_enabled_engine(env, store):
    """The user-facing path: enable the Compress engine in Engine Settings
    -> responses compress without any env change."""
    middleware, _ = _make_app(engine_active=True, env=env, store=store)
    messages = _drive(middleware, "/page")
    start = _start(messages)
    assert _header_of(start, "content-encoding") == "gzip"


def test_http_compression_records_stats():
    from engines import http_compress

    http_compress.reset_stats()
    middleware, _ = _make_app(env_on=True)
    _drive(middleware, "/page")
    _drive(middleware, "/binary")
    stats = http_compress.stats()
    assert stats["http_responses"] == 1
    assert stats["http_bytes_in"] > 4000
    assert stats["http_bytes_saved"] > 0
    assert stats["http_skipped"] == 1


def test_compression_engine_console_vs_core_preconditions():
    from engines.engines.compression import CompressionEngine

    bus, store = EventBus(), EngineStateStore("/tmp/emunel-cc")
    console_cfg = parse_env("console")
    core_cfg = parse_env("core")
    console_engine = CompressionEngine(console_cfg, bus, store)
    core_engine = CompressionEngine(core_cfg, bus, store)
    assert console_engine.preconditions() is None          # real channel
    assert core_engine.preconditions() and "cannot decompress" in core_engine.preconditions()
    # HOSTS covers both hosts now
    assert CompressionEngine.HOSTS == frozenset({"core", "console"})


def test_compression_engine_surfaces_http_stats(env, store):
    from engines import http_compress
    from engines.engines.compression import CompressionEngine

    http_compress.reset_stats()
    middleware, _ = _make_app(env_on=True)
    _drive(middleware, "/page")
    engine = CompressionEngine(parse_env("console"), EventBus(), store)
    asyncio.run(engine.init({}))
    metrics = engine.snapshot_metrics()
    assert metrics["http_responses"] == 1
    params = engine.defaults()
    assert "http_activation" in params


# ── REALITY reachability probe + baked Xray ─────────────────────────────────
def _stub_admin(monkeypatch):
    from emunel_console.auth import sessions as sessions_mod

    class _User:
        is_admin = True
        name = "test"
        login = "test"

    async def _get_user(pool, request):
        return _User()

    monkeypatch.setattr(sessions_mod, "get_session_user", _get_user)
    monkeypatch.setattr(sessions_mod, "require_admin", lambda user: None)
    monkeypatch.setattr(sessions_mod, "check_csrf", lambda request, user: None)


def _reality_client(monkeypatch, env, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from engines.api import build_router

    _stub_admin(monkeypatch)
    from emunel_console import db as console_db
    monkeypatch.setattr(console_db, "get_pool", lambda request: None)

    engine = make_reality(env, store)
    engine.status.active = True

    class StubManager:
        engines = {"Reality": engine}
        cfg = env

    app = FastAPI()
    app.include_router(build_router(StubManager()))
    return TestClient(app), engine


def test_reality_generate_labels_unreachable_endpoint(tmp_path, monkeypatch):
    """The honest fix for 'REALITY configs don't ping': with no server at
    address:port the response says so — instead of the user finding out in
    v2rayNG."""
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    env = parse_env("test")
    store = EngineStateStore(str(tmp_path / "s"))
    client, engine = _reality_client(monkeypatch, env, store)

    res = client.post("/api/engines/reality/generate",
                      json={"transport": "raw", "host": "127.0.0.1",
                            "port": 59999})            # nothing listens there
    assert res.status_code == 200
    body = res.json()
    assert body["share_url"].startswith("vless://")
    assert body["server_reachable"] is False
    assert "WILL NOT" in body["warning"] or "nothing is listening" in body["warning"] \
        or "NOT connect" in body["warning"]
    assert "59999" in body["endpoint"]


def test_reality_generate_confirms_reachable_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    env = parse_env("test")
    store = EngineStateStore(str(tmp_path / "s"))
    client, engine = _reality_client(monkeypatch, env, store)

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    threading.Thread(target=_accept_once, args=(server,), daemon=True).start()
    try:
        res = client.post("/api/engines/reality/generate",
                          json={"transport": "raw", "host": "127.0.0.1",
                                "port": port})
        assert res.status_code == 200
        body = res.json()
        assert body["server_reachable"] is True
        assert body["warning"] == ""
    finally:
        server.close()


def _accept_once(server):
    try:
        conn, _ = server.accept()
        try:
            conn.recv(64)
        except OSError:
            pass
        conn.close()
    except OSError:
        pass


def test_reality_generate_honors_public_host_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("REALITY_PUBLIC_HOST", "tcp.example.railway.app:12345")
    env = parse_env("test")
    assert env.reality_public_host == "tcp.example.railway.app:12345"
    store = EngineStateStore(str(tmp_path / "s"))
    client, engine = _reality_client(monkeypatch, env, store)
    # host omitted from the body -> REALITY_PUBLIC_HOST wins (probe fails fast,
    # warning names the endpoint)
    res = client.post("/api/engines/reality/generate", json={"transport": "raw"})
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"].startswith("tcp.example.railway.app")
    assert body["server_reachable"] is False


def test_baked_xray_detection(tmp_path, monkeypatch):
    import engines.engines.reality as reality_mod

    # absent by default
    monkeypatch.setattr(reality_mod, "BAKED_XRAY_PATH", str(tmp_path / "nope"))
    assert baked_xray() is None
    # present with a valid digest file -> recognized
    binary = tmp_path / "xray"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    digest = "a" * 64
    (tmp_path / "xray.sha256").write_text(digest)
    monkeypatch.setattr(reality_mod, "BAKED_XRAY_PATH", str(binary))
    assert baked_xray() == (str(binary), digest)
    # engine sees the runtime as configured (env pin would win over it)
    env = parse_env("test")
    engine = make_reality(env, EngineStateStore(str(tmp_path / "s2")))
    assert engine.runtime_source() == "image"
    assert engine.runtime_configured() is True


# ── panel wiring: rename + set-count + compression entry point ───────────────
def test_panel_has_config_rename_and_count_controls():
    page = (ROOT / "console" / "api" / "emunel_console" / "panel.py").read_text(
        encoding="utf-8")
    # rename: Name field in the edit drawer, PATCH includes label
    assert '<label>Name</label><input class="inp ed-n"' in page
    assert "if(nv)body.label=nv" in page
    # set-count: input + apply + confirm-delta logic
    assert 'id="cf-cnt"' in page
    assert 'id="cf-set"' in page
    assert "function applyCount()" in page


def test_panel_bypass_shows_reachability_and_baked_hint():
    page = (ROOT / "console" / "api" / "emunel_console" / "panel.py").read_text(
        encoding="utf-8")
    assert "server_reachable" in page
    assert "WILL NOT CONNECT" in page
    assert "XRAY_VERSION" in page                 # baked-Xray how-to
    assert "REALITY_PUBLIC_HOST" in page


def test_env_example_documents_new_knobs():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for knob in ("SNI_ENHANCED_ENABLED", "SNI_ENHANCED_METHOD",
                 "SNI_ENHANCED_FOOLING", "SNI_ENHANCED_MULTISPLIT_SEQOVL",
                 "SNI_ENHANCED_FRAGMENT_DELAY", "SNI_ENHANCED_TTL_VALUE",
                 "SNI_ENHANCED_SCAN_INTERVAL_HOURS", "SNI_ENHANCED_FINGERPRINT",
                 "EMUNEL_HTTP_COMPRESSION", "REALITY_PUBLIC_HOST"):
        assert knob in text, knob
