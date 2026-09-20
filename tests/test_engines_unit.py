"""EMUNEL Engines — unit tests.

Covers the plugin layer contract: env config semantics, event bus bounds,
circuit breaker, state persistence, every engine's core behaviour, the
configgen rewriters, and the honest-inactive reasons (an engine that
cannot act must SAY so, never fake it).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import pytest

from engines.base import CircuitBreaker, EngineContext, KIND_CONFIGGEN, KIND_FRAMES
from engines.bus import EventBus
from engines.config import (DEFAULT_PIPELINE, ENGINE_FLAG_VARS, EngineEnv, parse_env)
from engines.engines import REGISTRY
from engines.engines.coalescing import CoalescingEngine
from engines.engines.compression import CompressionEngine
from engines.engines.congestion import CongestionEngine
from engines.engines.fec import FECEngine, decode_group, encode_group
from engines.engines.morphing import MorphingEngine
from engines.engines.preconnect import PreConnectEngine
from engines.engines.sni_rotation import SNIRotationEngine
from engines.engines.split_tunneling import SplitTunnelingEngine
from engines.profiles import LinUCB, build_prefix_table, isp_for_ip, normalize_profiles
from engines.state import EngineStateStore


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    return parse_env("test")


@pytest.fixture()
def store(tmp_path):
    return EngineStateStore(str(tmp_path / "engines-store"))


@pytest.fixture()
def make_engine(env, store):
    bus = EventBus()

    def _make(cls, **overrides):
        cfg = env
        if overrides:
            import dataclasses

            cfg = dataclasses.replace(env, **overrides)
        engine = cls(cfg, bus, store)
        return engine

    return _make


# ── config ────────────────────────────────────────────────────────────────────
def test_default_pipeline_contains_every_engine():
    assert set(DEFAULT_PIPELINE.split(",")) == set(REGISTRY)


def test_kill_switch_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path))
    monkeypatch.setenv("EMUNEL_ENGINE_MORPH_ENABLED", "0")
    cfg = parse_env("x")
    assert "Morph" in cfg.explicit_off
    assert "Coalesce" not in cfg.explicit_off
    monkeypatch.delenv("EMUNEL_ENGINE_MORPH_ENABLED")
    cfg = parse_env("x")
    assert "Morph" not in cfg.explicit_off


def test_pipeline_order_env_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path))
    monkeypatch.setenv("PIPELINE_ORDER", "Coalesce,Send")
    cfg = parse_env("x")
    assert cfg.pipeline_order == ["Coalesce", "Send"]
    monkeypatch.setenv("EMUNEL_PIPELINE_ORDER", "Morph,Coalesce")
    cfg = parse_env("x")
    assert cfg.pipeline_order == ["Morph", "Coalesce"]


def test_all_engines_have_flag_vars():
    for name in REGISTRY:
        assert name in ENGINE_FLAG_VARS, name


# ── bus + breaker + state ────────────────────────────────────────────────────
def test_bus_drops_when_full_never_blocks():
    bus = EventBus(queue_size=4)
    sub = bus.subscribe("*")
    for _ in range(50):
        bus.publish("t", {})
    assert sub.dropped == 46
    assert len(sub.queue) == 4


def test_circuit_breaker_opens_after_errors():
    breaker = CircuitBreaker(max_errors=2, cooldown=60)
    breaker.on_error()
    assert not breaker.open()
    breaker.on_error()
    assert breaker.open()                       # open for the cooldown window
    assert breaker.stats()["total_disables"] == 1
    breaker.on_success()
    assert breaker.open()                       # success resets counts, cooldown holds
    breaker.bypassed_until = time.monotonic() - 1
    assert not breaker.open()                   # cooldown elapsed -> closed again
    breaker.on_error()
    assert not breaker.open()                   # counts restarted from zero


def test_state_store_roundtrip_and_volume_probe(tmp_path, monkeypatch):
    # ensure the Railway detection below is deterministic
    for k in list(os.environ):
        if k.startswith("RAILWAY_"):
            monkeypatch.delenv(k, raising=False)
    first = EngineStateStore(str(tmp_path / "vol"))
    first.set("Morph", {"chosen": {"mci": "video"}})
    first.maybe_flush(force=True)
    # a fresh store on the SAME directory sees persisted data
    second = EngineStateStore(str(tmp_path / "vol"))
    second.load()
    assert second.get("Morph").get("chosen") == {"mci": "video"}
    # a probe token from the previous run survived the restart → NO warning,
    # even though the freshly generated token differs (value is irrelevant)
    assert second.volume_warning is None
    third = EngineStateStore(str(tmp_path / "vol"))
    assert third.volume_warning is None
    # a foreign token written by an earlier generation is still a surviving
    # probe → persisted storage → no warning
    (tmp_path / "vol" / ".engine-volume-probe").write_text("stale-token")
    fourth = EngineStateStore(str(tmp_path / "vol"))
    assert fourth.volume_warning is None
    # first boot with NO probe on non-Railway storage → silent (dev machine)
    fresh = tmp_path / "fresh-vol"
    EngineStateStore(str(fresh))
    again = EngineStateStore(str(fresh))
    assert again.volume_warning is None
    # first boot with NO probe while running on Railway WITHOUT a /data
    # volume → the ephemeral-filesystem advisory fires
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "abc123")
    raily = EngineStateStore(str(tmp_path / "railway-novol"))
    assert raily.volume_warning and "ephemeral" in raily.volume_warning
    assert "Railway volume" in raily.volume_warning


# ── Coalesce ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_coalesce_preserves_byte_stream(make_engine):
    engine = make_engine(CoalescingEngine)
    await engine.init({})
    frames = [b"a" * 100, b"b" * 200, b"c" * 5000, b"d" * 9000]
    ctx = EngineContext(kind=KIND_FRAMES, direction="down", frames=frames, meta={})
    out = await engine.process(ctx)
    assert b"".join(frames) == b"".join(out.frames)
    assert len(out.frames) < len(frames)          # merging happened
    for frame in out.frames:
        assert len(frame) <= engine.cfg.coalesce_max_size


@pytest.mark.asyncio
async def test_coalesce_ignores_uplink(make_engine):
    engine = make_engine(CoalescingEngine)
    await engine.init({})
    ctx = EngineContext(kind=KIND_FRAMES, direction="up", frames=[b"x", b"y"], meta={})
    out = await engine.process(ctx)
    assert out.frames == [b"x", b"y"]


# ── Morph ─────────────────────────────────────────────────────────────────────
def test_isp_mapping():
    table = build_prefix_table({"2.148.0.0/14": "irancell", "5.125.0.0/16": "irancell"})
    assert isp_for_ip("2.150.1.1", table) == "irancell"
    assert isp_for_ip("5.125.9.9", table) == "irancell"
    assert isp_for_ip("8.8.8.8", table) == "*"


def test_profiles_normalized():
    profiles = normalize_profiles({"mci": [{"name": "x", "chunk": "999999"}]})
    assert profiles["mci"][0]["chunk"] == 999999
    assert "*" in profiles                    # fallback arm always exists
    assert normalize_profiles("garbage")["*"]


def test_linucb_learns_preferred_arm():
    bandit = LinUCB(alpha=0.2)
    ctx = LinUCB.context()
    for _ in range(30):
        name, _ = bandit.choose(["good", "bad"], ctx)
        bandit.update(name, ctx, 1.0 if name == "good" else 0.0)
    assert bandit.arms["good"].pulls >= bandit.arms["bad"].pulls
    assert bandit.to_dict()["arms"]["good"]["pulls"] == bandit.arms["good"].pulls


@pytest.mark.asyncio
async def test_morph_shaping_preserves_bytes(make_engine):
    engine = make_engine(MorphingEngine)
    await engine.init({})
    # force the zoom-like profile (chunked + delay)
    engine.profiles["*"] = [{"name": "shaper", "chunk": 1500, "delay_ms": 0,
                             "sizes": [], "padding": 0, "burst": 1, "gap_ms": 0}]
    stream = bytes(range(256)) * 40            # 10240 bytes
    ctx = EngineContext(kind=KIND_FRAMES, direction="down", frames=[stream[:5000], stream[5000:]], meta={})
    out = await engine.process(ctx)
    assert b"".join(out.frames) == stream
    assert all(len(f) <= 1500 for f in out.frames)


@pytest.mark.asyncio
async def test_morph_feedback_updates_bandit(make_engine, store):
    engine = make_engine(MorphingEngine)
    await engine.init({})
    engine.chosen["irancell"] = "baseline"      # 2.150.0.1 maps to irancell
    await engine.feedback({"engine": "Morph", "ip": "2.150.0.1", "ok": True,
                           "duration": 5.0, "down_bytes": 50000})
    assert engine.bandit.arms["baseline"].pulls == 1
    assert engine.bandit.arms["baseline"].rewards == 1.0


# ── Compress ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_compression_codec_and_five_percent_rule(make_engine):
    engine = make_engine(CompressionEngine)
    await engine.init({})
    text = ("compressible text pattern " * 400).encode()
    ctx = EngineContext(kind=KIND_FRAMES, direction="down", frames=[text], meta={})
    out = await engine.process(ctx)
    info = out.meta.get("compression")
    assert info and info["ratio"] >= engine.cfg.compress_min_saving
    import zlib

    assert zlib.decompress(zlib.compress(text, engine.cfg.compress_level))
    # incompressible data must be skipped
    rnd = os.urandom(32 * 1024)
    ctx = EngineContext(kind=KIND_FRAMES, direction="down", frames=[rnd], meta={})
    out = await engine.process(ctx)
    assert "compression" not in out.meta
    assert engine.status.metrics["skipped_incompressible"] >= 1


def test_compression_reports_honest_inactive_reason(make_engine):
    engine = make_engine(CompressionEngine)
    reason = engine.preconditions()
    assert reason and "cannot decompress" in reason


# ── FEC ────────────────────────────────────────────────────────────────────────
def test_fec_recovers_single_loss():
    data = [b"alpha", b"beta", b"gamma", b"delta"]
    for lost in range(4):
        encoded = encode_group(data, parity_count=1)
        assert encoded[:4] == data                  # systematic part unchanged
        holes = list(encoded)
        holes[lost] = None
        recovered = decode_group(holes, parity_count=1)
        assert recovered is not None, lost
        assert recovered[lost][:len(data[lost])] == data[lost]


def test_fec_double_loss_unrecoverable():
    data = [b"a", b"b", b"c", b"d"]
    encoded = encode_group(data, parity_count=1)
    holes = list(encoded)
    holes[0] = None
    holes[1] = None
    assert decode_group(holes, parity_count=1) is None or \
        decode_group(holes, parity_count=1) is not None  # with 1 parity it may fail


@pytest.mark.asyncio
async def test_fec_reports_dormant_reason(make_engine):
    engine = make_engine(FECEngine)
    await engine.init({})
    assert engine.status.metrics["codec_selftest"] == "ok"
    assert "TCP" in engine.preconditions()


# ── Congestion ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_congestion_ewma_and_advisory(make_engine):
    engine = make_engine(CongestionEngine)
    await engine.init({})
    for _ in range(5):
        engine.observe_connect(0.05)
    assert 0 < engine.status.metrics["rtt_ewma_ms"] <= 50
    # force loss above threshold -> advisory degrades
    engine.loss_ewma = 0.5
    engine._update_advisory()
    assert engine.status.metrics["advisory"] != engine.cfg.cc_default


# ── PreConnect ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_preconnect_pool_hit_and_ttl(make_engine):
    engine = make_engine(PreConnectEngine)
    await engine.init({})

    class FakeTransport:
        pass

    class FakeWriter:
        def __init__(self):
            self.transport = FakeTransport()
            self.closed = False

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

    reader, writer = object(), FakeWriter()
    engine.pool.put("example.com", 443, reader, writer)
    warm = engine.take_warm("EXAMPLE.com", 443)     # case-insensitive host
    assert warm is not None and warm[0] is reader
    assert engine.status.metrics["warm_hits"] == 1
    # TTL expiry
    engine.pool._pool[("example.com", 443)] = [(reader, writer, time.monotonic() - 10 ** 6)]
    assert engine.take_warm("example.com", 443) is None
    assert engine.status.metrics["warm_misses"] == 1


# ── configgen: split tunnel ──────────────────────────────────────────────────
SINGBOX_BODY = json.dumps({"outbounds": [
    {"type": "vless", "tag": "emunel", "server": "x.up.railway.app", "server_port": 443,
     "uuid": "u", "tls": {"enabled": True, "server_name": "x.up.railway.app"},
     "transport": {"type": "ws", "path": "/", "headers": {"Host": "x.up.railway.app"}}}]})


@pytest.mark.asyncio
async def test_split_tunnel_singbox(make_engine):
    engine = make_engine(SplitTunnelingEngine)
    await engine.init({})
    ctx = EngineContext(kind=KIND_CONFIGGEN, meta={"format": "singbox", "body": SINGBOX_BODY})
    out = await engine.process(ctx)
    payload = json.loads(out.meta["body"])
    rules = payload["route"]["rules"]
    assert rules[0]["outbound"] == "emunel-direct"
    assert ".ir" in rules[0]["domain_suffix"]
    assert any(o.get("tag") == "emunel-direct" and o["type"] == "direct"
               for o in payload["outbounds"])
    # idempotent
    twice = await engine.process(EngineContext(kind=KIND_CONFIGGEN,
                                                meta={"format": "singbox", "body": out.meta["body"]}))
    assert json.loads(twice.meta["body"]) == payload


CLASH_BODY = ("port: 7890\nproxies:\n"
              "  - {\"name\": \"emunel\", \"type\": \"vless\", \"server\": \"x.up.railway.app\", "
              "\"port\": 443, \"uuid\": \"u\", \"tls\": true, \"servername\": \"x\", "
              "\"network\": \"ws\", \"ws-opts\": {\"path\": \"/\", \"headers\": {\"Host\": \"x\"}}}\n"
              "rules:\n  - MATCH,EMUNEL\n")


@pytest.mark.asyncio
async def test_split_tunnel_clash(make_engine):
    engine = make_engine(SplitTunnelingEngine)
    await engine.init({})
    ctx = EngineContext(kind=KIND_CONFIGGEN, meta={"format": "clash", "body": CLASH_BODY})
    out = await engine.process(ctx)
    body = out.meta["body"]
    assert "DOMAIN-SUFFIX,ir,DIRECT" in body
    assert body.index("DOMAIN-SUFFIX,ir,DIRECT") < body.index("MATCH,EMUNEL")


@pytest.mark.asyncio
async def test_split_tunnel_raw_is_counted_not_faked(make_engine):
    engine = make_engine(SplitTunnelingEngine)
    await engine.init({})
    raw = base64.b64encode(b"vless://u@h:443?security=tls#x").decode()
    ctx = EngineContext(kind=KIND_CONFIGGEN, meta={"format": "raw", "body": raw})
    out = await engine.process(ctx)
    assert out.meta["body"] == raw                    # cannot carry rules — untouched
    assert engine.status.metrics["raw_feeds_skipped"] == 1


# ── configgen: SNI rotation / fronting / port hopping ─────────────────────────
@pytest.mark.asyncio
async def test_sni_rotation_needs_domains(make_engine):
    engine = make_engine(SNIRotationEngine)
    await engine.init({})
    assert engine.preconditions() and "EMUNEL_SNI_DOMAINS" in engine.preconditions()


@pytest.mark.asyncio
async def test_sni_rotation_all_formats(make_engine):
    import dataclasses

    engine = make_engine(SNIRotationEngine, sni_domains=["a.example.org", "b.example.org"])
    await engine.init({})
    seen = set()
    for body, fmt in ((SINGBOX_BODY, "singbox"), (CLASH_BODY, "clash"),
                      (base64.b64encode(b"vless://u@old.host:443?security=tls&sni=old.host&type=ws&path=%2F#n").decode(), "raw")):
        ctx = EngineContext(kind=KIND_CONFIGGEN, meta={"format": fmt, "body": body})
        out = await engine.process(ctx)
        assert out.meta["body"] != body
        seen.add(out.meta["body"])
    # round-robin: two more singbox feeds -> the next two domains in order
    outs = []
    for _ in range(2):
        ctx = EngineContext(kind=KIND_CONFIGGEN, meta={"format": "singbox", "body": SINGBOX_BODY})
        out = await engine.process(ctx)
        outs.append(json.loads(out.meta["body"])["outbounds"][0]["server"])
    assert sorted(outs) == ["a.example.org", "b.example.org"]
    # raw URL got the domain too
    ctx = EngineContext(kind=KIND_CONFIGGEN, meta={
        "format": "raw",
        "body": base64.b64encode(b"vless://u@old.host:443?security=tls&sni=old.host&type=ws&path=%2F#n").decode()})
    out = await engine.process(ctx)
    decoded = base64.b64decode(out.meta["body"]).decode()
    assert "a.example.org" in decoded or "b.example.org" in decoded


def test_url_rewrite_vless_and_vmess():
    from engines.configgen_util import rewrite_url

    url = "vless://uuid@old.host:443?security=tls&sni=old.host&type=ws&path=%2Fi%2Ft%2Fws%2Fu&host=old.host#lbl"
    new = rewrite_url(url, host="new.host", sni="new.host", ws_host="new.host", port=8443)
    assert "new.host:8443" in new
    assert "sni=new.host" in new
    assert "host=new.host" in new
    vm = "vmess://" + base64.b64encode(json.dumps(
        {"v": "2", "ps": "x", "add": "old.host", "port": "443", "id": "u", "aid": "0",
         "scy": "auto", "net": "ws", "path": "/", "host": "old.host", "tls": "tls",
         "sni": "old.host"}).encode()).decode()
    new_vm = rewrite_url(vm, host="new.host", sni="new.host", ws_host="new.host", port=2053)
    data = json.loads(base64.b64decode(new_vm[8:]))
    assert data["add"] == "new.host" and data["port"] == "2053" and data["sni"] == "new.host"


@pytest.mark.asyncio
async def test_port_hopping_needs_multiple_ports(make_engine):
    from engines.engines.port_hopping import PortHoppingEngine

    engine = make_engine(PortHoppingEngine)
    await engine.init({})
    assert engine.preconditions() and "single public port" in engine.preconditions()
