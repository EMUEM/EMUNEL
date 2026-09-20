"""Stability fixes — regression tests.

Covers the incident reported against the Railway deployment:
  1. engine hot-toggles resetting after every restart (Morph flipping off)
  2. the mobile bottom nav being dead CSS (Bypass/Engines undiscoverable)
  3. panel polling hammering a struggling server (no backoff/overlap guard)
  4. the gateway burning two DB queries + 2-3 INFO log lines per proxied
     request and a fresh httpx.AsyncClient per hop (memory/CPU climb)
  5. the rate limiter existing but never being wired into the app
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))

from engines.bus import EventBus
from engines.config import parse_env
from engines.manager import EngineManager
from engines.state import EngineStateStore

# Captured at COLLECTION time: later test modules replace the gateway's
# module attributes during their runs (without restoring), so any reference
# resolved at test time could be someone else's stub.
from emunel_console.services import gateway as _gateway_module
_PRISTINE_RESOLVE = _gateway_module._resolve_endpoint


# ── 1. engine toggle persistence across restarts ─────────────────────────────
@pytest.mark.asyncio
async def _manager_with(data_dir: str, **cfg_overrides):
    import dataclasses

    cfg = parse_env("console")
    cfg = dataclasses.replace(cfg, **cfg_overrides) if cfg_overrides else cfg
    state = EngineStateStore(data_dir)
    manager = EngineManager("console", cfg=cfg, bus=EventBus(), state=state)
    await manager.start()
    return manager


@pytest.mark.asyncio
async def test_hot_enable_survives_manager_restart(tmp_path):
    """Morph turning itself back off after a restart was the reported bug."""
    data = str(tmp_path / "engines")

    # simulate the OLD default (off) to prove persistence does the work
    m1 = await _manager_with(data, morph_on=False)
    assert "Morph" not in m1.active_engine_names()
    ok, message = await m1.set_engine_enabled("Morph", True)
    assert ok, message
    assert "Morph" in m1.active_engine_names()
    await m1.stop()

    # "restart": a brand-new manager over the same state directory
    m2 = await _manager_with(data, morph_on=False)
    assert "Morph" in m2.active_engine_names(), \
        "persisted hot-enable must be re-applied on boot"
    await m2.stop()


@pytest.mark.asyncio
async def test_hot_disable_survives_manager_restart(tmp_path):
    data = str(tmp_path / "engines")

    m1 = await _manager_with(data)
    assert "Coalesce" in m1.active_engine_names()
    ok, _ = await m1.set_engine_enabled("Coalesce", False)
    assert ok
    assert "Coalesce" not in m1.active_engine_names()
    await m1.stop()

    m2 = await _manager_with(data)   # Coalesce default is ON
    assert "Coalesce" not in m2.active_engine_names(), \
        "a persisted disable must beat the engine's on-by-default flag"
    engine = m2.engines["Coalesce"]
    assert "persisted" in engine.status.reason
    await m2.stop()


@pytest.mark.asyncio
async def test_env_kill_switch_beats_persisted_enable(tmp_path, monkeypatch):
    data = str(tmp_path / "engines")
    monkeypatch.setenv("EMUNEL_ENGINE_MORPH_ENABLED", "0")
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", data)

    m1 = await _manager_with(data, morph_on=False)
    # an explicit env =0 blocks even the hot toggle
    ok, _message = await m1.set_engine_enabled("Morph", True)
    assert not ok
    await m1.stop()

    m2 = await _manager_with(data, morph_on=False)
    engine = m2.engines.get("Morph")
    assert engine is None or "disabled by env" in engine.status.reason
    assert "Morph" not in m2.active_engine_names()
    await m2.stop()


def test_morph_now_on_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path))
    cfg = parse_env("console")
    assert cfg.morph_on is True, \
        "Morph's default profile is a byte-exact passthrough — it ships ON"


# ── 2/3. served panel: mobile nav + resilient polling ────────────────────────
def test_mobile_bottom_nav_css_is_alive():
    """The stray .bnav{display:none} used to sit AFTER the media query and
    killed the mobile nav entirely (Engines/Bypass unreachable on phones)."""
    from emunel_console import panel

    idx_hidden = panel.PAGE.find(".bnav{display:none}")
    idx_media = panel.PAGE.find("@media(max-width:840px)")
    assert idx_hidden > 0 and idx_media > 0
    assert idx_hidden < idx_media, \
        ".bnav{display:none} must come BEFORE the media query that turns it on"

    # and the media query itself must contain exactly one display:flex for .bnav
    media_block = panel.PAGE[idx_media:idx_media + 4000]
    assert ".bnav{display:flex" in media_block
    # no second, overriding .bnav{display:none} after the media block
    assert ".bnav{display:none}" not in panel.PAGE[idx_media:]


def test_panel_polling_is_backoff_safe():
    from emunel_console import panel

    assert "function poll(" in panel.PAGE
    assert "Math.pow(2,fail)" in panel.PAGE, "exponential backoff on failures"
    assert "document.hidden" in panel.PAGE, "pause while the tab is hidden"
    assert "pollTimer=every(" not in panel.PAGE, "old fixed-interval polling gone"
    # visible connection state instead of silent page breakage
    assert "noteNetErr" in panel.PAGE


def test_engines_page_groups_by_host():
    from emunel_console import panel

    assert "egGroups" in panel.PAGE
    assert "Runs on this deployment" in panel.PAGE
    assert "Runs inside each proxy instance" in panel.PAGE
    assert "Off / needs configuration" in panel.PAGE


# ── 4. gateway: endpoint resolve cache ───────────────────────────────────────
class _Row(dict):
    def __getitem__(self, key):                     # asyncpg-Record-ish
        return dict.__getitem__(self, key)


class _FakePool:
    def __init__(self, row=None):
        self.row = row
        self.fetchrow_calls = 0

    async def fetchrow(self, *_a, **_k):
        self.fetchrow_calls += 1
        return self.row


class _FakeRequest:
    def __init__(self, pool):
        self._pool = pool

    @property
    def headers(self):
        return {}


@pytest.mark.asyncio
async def test_resolve_endpoint_caches_hits_and_misses(tmp_path, monkeypatch):
    from emunel_console.services import gateway as gw

    monkeypatch.setattr(gw, "_resolve_endpoint", _PRISTINE_RESOLVE)
    monkeypatch.setenv("EMUNEL_ENDPOINT_CACHE_SECONDS", "15")
    running_row = _Row({"id": "i" * 32, "status": "running",
                         "endpoint_token": "tok-active", "node_id": "local"})
    pool = _FakePool(running_row)
    monkeypatch.setattr(gw, "get_pool", lambda request: pool)

    req = _FakeRequest(pool)
    t1 = await gw._resolve_endpoint(req, "tok-active")
    t2 = await gw._resolve_endpoint(req, "tok-active")
    assert t1 == t2 and t1 is not None
    assert pool.fetchrow_calls == 1, "second resolve must be served from cache"

    # lifecycle invalidation drops the entry
    gw.invalidate_endpoint_cache(t1["instance_id"])
    t3 = await gw._resolve_endpoint(req, "tok-active")
    assert t3 is not None
    assert pool.fetchrow_calls == 2

    # dead endpoint: negative result cached too (reconnect storms hit RAM,
    # not the database)
    dead_pool = _FakePool(None)
    monkeypatch.setattr(gw, "get_pool", lambda request: dead_pool)
    assert await gw._resolve_endpoint(req, "tok-dead") is None
    assert await gw._resolve_endpoint(req, "tok-dead") is None
    assert dead_pool.fetchrow_calls == 1


@pytest.mark.asyncio
async def test_worker_client_is_one_per_event_loop():
    from emunel_console.services import gateway as gw

    a = gw._worker_client()
    b = gw._worker_client()
    assert a is b, "one shared client inside a single event loop"

    def _in_other_loop():
        async def _main():
            client = gw._worker_client()
            other = gw._worker_client()
            assert other is client, "same client within the second loop too"
            await client.aclose()

        asyncio.run(_main())

    await asyncio.to_thread(_in_other_loop)
    # the thread's loop had its own client — ours is untouched and still open
    assert not a.is_closed


# ── 5. rate limiting: wired, auth+api only, never the proxy path ─────────────
def test_rate_limiter_window():
    from emunel_console.security.ratelimit import RateLimiter, Rule

    rl = RateLimiter()
    rule = Rule("t", limit=3, window_seconds=60)
    for _ in range(3):
        rl.check("k", rule)
    with pytest.raises(Exception):
        rl.check("k", rule)
    # other keys unaffected
    rl.check("other", rule)


@pytest.mark.asyncio
async def test_rate_limit_asgi_auth_only_and_gateway_never():
    from emunel_console.security.ratelimit import RULES, RateLimitASGI

    hits = []

    async def app(scope, receive, send):
        hits.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = RateLimitASGI(app)

    async def post(path, extra_headers=()):
        status = []

        async def send(message):
            if message["type"] == "http.response.start":
                status.append(message["status"])

        scope = {"type": "http", "path": path, "method": "POST",
                 "headers": [(b"host", b"x"), (b"content-type", b"application/json"),
                             (b"x-forwarded-for", b"9.9.9.9")] + list(extra_headers),
                 "client": ("9.9.9.9", 1234)}
        await mw(scope, None, send)
        return status[0]

    # auth bucket: limit+1 requests -> the last one is a 429 with Retry-After
    limit = RULES["auth"].limit
    codes = [await post("/auth/login-password") for _ in range(limit + 1)]
    assert codes[:-1] == [200] * limit
    assert codes[-1] == 429

    # GETs on auth endpoints (the panel's /auth/me) are not throttled
    for _ in range(5):

        async def send(message):
            pass

        scope = {"type": "http", "path": "/auth/me", "method": "GET",
                 "headers": [(b"host", b"x")], "client": ("9.9.9.9", 1)}
        await mw(scope, None, send)

    # the proxy gateway is NEVER rate limited — xHTTP packet-up clients and
    # carrier-NAT users legitimately exceed any per-IP ceiling
    codes = [await post("/i/someendpoint/xhttp/up") for _ in range(60)]
    assert codes == [200] * 60


@pytest.mark.asyncio
async def test_rate_limit_asgi_streaming_passthrough():
    """The middleware must be pure ASGI: response bodies stream through
    untouched (no buffering) — xHTTP stream-up depends on it."""
    from emunel_console.security.ratelimit import RateLimitASGI

    chunks = [b"one", b"two", b"three"]

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for c in chunks:
            await send({"type": "http.response.body", "body": c, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    seen = []

    async def send(message):
        seen.append(message)

    mw = RateLimitASGI(app)
    scope = {"type": "http", "path": "/api/whatever", "method": "GET",
             "headers": [(b"host", b"x")], "client": ("8.8.8.8", 5)}
    await mw(scope, None, send)
    assert [m.get("body") for m in seen[1:-1]] == chunks
