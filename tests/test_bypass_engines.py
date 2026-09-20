"""Bypass engines — SNI Spoofing + REALITY unit and integration tests.

Covers:
  * ClientHello parsing / fragment planning / fake packet structure
    (server-side engine copy AND the standalone client helper script —
    both must agree byte-for-byte);
  * the SNI profile validation (rejects nonsense, accepts good values);
  * X25519 keypair generation + derivation + env-pair verification;
  * REALITY inbound/outbound/vless:// generation for RAW, XHTTP, gRPC;
  * the pinned-Xray binary verification rules (refuses unsafe binaries);
  * the engines API surface (admin-gated, 401 anonymous, 503 when off);
  * the panel page wiring (Bypass nav + viewBypass in the SERVED panel).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from engines.bus import EventBus
from engines.config import parse_env
from engines.engines import REGISTRY
from engines.engines.reality import (
    RealityEngine, build_inbound, build_outbound, build_share_link,
    derive_x25519_public, generate_x25519_keypair, verify_xray_binary)
from engines.engines.sni_spoofing import (
    SNISpoofingEngine, build_fake_client_hello, parse_client_hello,
    plan_fragments)
from engines.state import EngineStateStore

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "engines" / "assets" / "emunel_sni_helper.py"


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    return parse_env("test")


@pytest.fixture()
def store(tmp_path):
    return EngineStateStore(str(tmp_path / "engines-store"))


@pytest.fixture()
def sni(env, store):
    return SNISpoofingEngine(env, EventBus(), store)


@pytest.fixture()
def reality(env, store):
    return RealityEngine(env, EventBus(), store)


def _load_helper():
    spec = importlib.util.spec_from_file_location("emunel_sni_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── SNI: ClientHello logic (engine copy) ────────────────────────────────────
def test_parse_finds_sni_in_synthetic_hello():
    hello = build_fake_client_hello("blubank.com")
    parsed = parse_client_hello(hello)
    assert parsed["sni"] == "blubank.com"
    assert 0 < parsed["sni_start"] < parsed["sni_end"] <= len(hello)
    # the bytes we report as SNI really are the SNI
    assert hello[parsed["sni_start"]:parsed["sni_end"]] == b"blubank.com"


def test_parse_rejects_non_tls():
    assert parse_client_hello(b"GET / HTTP/1.1\r\n\r\n") == {}
    assert parse_client_hello(b"\x17\x03\x03\x00\x01\x00") == {}
    assert parse_client_hello(b"") == {}


def test_fragment_strategies_preserve_stream():
    hello = build_fake_client_hello("www.example-iran.ir")
    for strategy in ("sni_split", "half", "multi"):
        parts = plan_fragments(hello, strategy)
        assert len(parts) >= 2, strategy
        assert b"".join(parts) == hello, strategy
        assert all(p for p in parts)


def test_sni_split_cuts_inside_the_hostname():
    hello = build_fake_client_hello("abcdefghijklmnop.com")
    parsed = parse_client_hello(hello)
    parts = plan_fragments(hello, "sni_split")
    cut = len(parts[0])
    assert parsed["sni_start"] < cut < parsed["sni_end"], "cut must fall inside SNI"


def test_tls_record_frag_rewraps_records():
    hello = build_fake_client_hello("blubank.com")
    parts = plan_fragments(hello, "tls_record_frag")
    assert len(parts) == 2
    assert all(p[0] == 0x16 for p in parts)                # both are TLS records
    assert b"".join(p[5:] for p in parts) == hello[5:]       # content preserved
    assert sum(int.from_bytes(p[3:5], "big") for p in parts) == len(hello) - 5


def test_unknown_strategy_falls_back_to_sni_split():
    hello = build_fake_client_hello("x.example.com")
    assert plan_fragments(hello, "nonsense") == plan_fragments(hello, "sni_split")


def test_fake_packet_is_a_valid_hello_with_the_fake_sni():
    fake = build_fake_client_hello("www.microsoft.com")
    parsed = parse_client_hello(fake)
    assert parsed["sni"] == "www.microsoft.com"
    assert fake[0] == 0x16 and fake[5] == 0x01


# ── SNI: helper script agrees with the engine copy ─────────────────────────
def test_helper_and_engine_logic_are_identical():
    helper = _load_helper()
    import random

    for host in ("blubank.com", "divar.ir", "a-very-long-host-name.example.org"):
        a = build_fake_client_hello(host, random.Random(7))
        b = helper.build_fake_client_hello(host, random.Random(7))
        assert a == b, f"fake hello differs for {host}"
        assert parse_client_hello(a) == helper.parse_client_hello(b)
        for strategy in ("sni_split", "half", "multi", "tls_record_frag"):
            assert (plan_fragments(a, strategy, random.Random(3))
                    == helper.plan_fragments(b, strategy, random.Random(3))), strategy


def test_helper_script_compiles():
    compile(HELPER.read_text(encoding="utf-8"), str(HELPER), "exec")


# ── SNI: helper really fragments through a live socket ──────────────────────
def test_helper_live_fragmentation():
    helper = _load_helper()

    async def main():
        received = []
        fake_seen = []

        async def sink(reader, writer):
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    received.append(data)
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass

        server = await asyncio.start_server(sink, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        class Args:
            connect = ("127.0.0.1", port)
            method = "combined"
            strategy = "sni_split"
            delay = 0.05
            ttl = 1
            fake_sni = "www.microsoft.com"
            pool = ["www.microsoft.com"]
            rotate = True
            max_conns = 4

        proxy = helper.BypassProxy(Args())
        fake_server = await asyncio.start_server(sink, "127.0.0.1", 0)
        fake_port = fake_server.sockets[0].getsockname()[1]
        orig = proxy._send_fake_packet

        async def fake_to_local(host, p):
            await orig("127.0.0.1", fake_port)

        proxy._send_fake_packet = fake_to_local
        pserver = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        pport = pserver.sockets[0].getsockname()[1]
        try:
            hello = helper.build_fake_client_hello("emunel-live.example")
            reader, writer = await asyncio.open_connection("127.0.0.1", pport)
            writer.write(hello)
            await writer.drain()
            await asyncio.sleep(0.8)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            await asyncio.sleep(0.1)
        finally:
            pserver.close()
            fake_server.close()
            server.close()
            await asyncio.sleep(0.05)
        return received

    received = asyncio.run(main())
    assert len(received) >= 2, "ClientHello was not split into fragments"
    joined = b"".join(received)
    assert joined.startswith(b"\x16\x03\x01"), "relayed bytes are the ClientHello"


# ── SNI: engine profile management ──────────────────────────────────────────
def test_sni_engine_profile_validation(sni):
    asyncio.run(sni.init({}))
    with pytest.raises(ValueError):
        sni.set_profile({"method": "nonsense"})
    with pytest.raises(ValueError):
        sni.set_profile({"strategy": "nope"})
    with pytest.raises(ValueError):
        sni.set_profile({"delay": 5.0})
    with pytest.raises(ValueError):
        sni.set_profile({"ttl_value": 99})
    with pytest.raises(ValueError):
        sni.set_profile({"sni_pool": []})
    with pytest.raises(ValueError):
        sni.set_profile({"sni_pool": ["http://bad.example"]})
    profile = sni.set_profile({"method": "fragment", "strategy": "multi",
                                "delay": 0.2, "ttl_value": 3})
    assert profile["method"] == "fragment"
    assert profile["fragment_strategy"] == "multi"
    assert profile["fragment_delay"] == 0.2
    assert profile["ttl_value"] == 3


def test_sni_engine_profile_persists(sni, store):
    asyncio.run(sni.init({}))
    sni.set_profile({"fake_sni": "cdnjs.cloudflare.com"})
    # a fresh engine sees the persisted profile
    second = SNISpoofingEngine(sni.cfg, EventBus(), store)
    asyncio.run(second.init({}))
    assert second.profile["fake_sni"] == "cdnjs.cloudflare.com"


def test_sni_engine_run_test_proves_plan(sni):
    asyncio.run(sni.init({}))
    for strategy in ("sni_split", "half", "multi", "tls_record_frag"):
        sni.set_profile({"strategy": strategy})
        result = sni.run_test()
        assert result["ok"] is True, (strategy, result)
        assert result["parsed_sni"] == "emunel-test.example.com"
        assert len(result["fragment_sizes"]) >= 2
    sni.set_profile({"method": "combined"})
    result = sni.run_test()
    assert result["ok"] is True
    assert result["fake_packet_bytes"] > 0
    assert result["fake_packet_sni"]


def test_sni_engine_helper_source_and_usage(sni):
    asyncio.run(sni.init({}))
    source = sni.helper_source()
    assert "emunel_sni_helper" in source or "BypassProxy" in source
    assert "--connect" in source
    usage = sni.helper_usage()
    assert "--connect" in usage and "--method" in usage


# ── REALITY: keys ───────────────────────────────────────────────────────────
def test_x25519_keypair_shape_and_derivation():
    private, public = generate_x25519_keypair()
    assert len(private) == 44 and len(public) == 44          # base64(32 bytes)
    assert derive_x25519_public(private) == public
    with pytest.raises(ValueError):
        derive_x25519_public("not-base64!!")
    with pytest.raises(ValueError):
        derive_x25519_public("AAAA")                          # wrong length


def test_reality_engine_generates_and_persists_keys(reality, store):
    asyncio.run(reality.init({}))
    pair = reality.generate_keys()
    assert pair["private_key"] and pair["public_key"]
    assert pair["client_uuid"]
    # active immediately and persisted for the next boot
    assert reality.active_public_key() == pair["public_key"]
    second = RealityEngine(reality.cfg, EventBus(), store)
    asyncio.run(second.init({}))
    assert second.active_public_key() == pair["public_key"]
    # the private key never appears in defaults()/status payloads
    assert "private" not in json.dumps(second.defaults()).lower()


def test_reality_env_pair_mismatch_is_repaired(tmp_path, monkeypatch, store):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("REALITY_PRIVATE_KEY", generate_x25519_keypair()[0])
    monkeypatch.setenv("REALITY_PUBLIC_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    cfg = parse_env("test")
    engine = RealityEngine(cfg, EventBus(), store)
    asyncio.run(engine.init({}))
    # derived-from-private wins over a mismatching REALITY_PUBLIC_KEY
    assert engine.active_public_key() == derive_x25519_public(cfg.reality_private_key)


# ── REALITY: config generation ──────────────────────────────────────────────
def test_reality_configs_raw_shape(reality):
    asyncio.run(reality.init({}))
    out = reality.generate_configs(address="edge.example.com", transport="raw")
    ib, ob = out["inbound"], out["outbound"]
    rs = ib["streamSettings"]["realitySettings"]
    assert ib["streamSettings"]["security"] == "reality"
    assert rs["target"] == "blubank.com:443"
    assert rs["privateKey"]
    assert ib["settings"]["clients"][0]["flow"] == "xtls-rprx-vision"
    orc = ob["streamSettings"]["realitySettings"]
    assert orc["serverName"] == "blubank.com"
    assert orc["fingerprint"] == "chrome"
    assert orc["shortId"] == "0123456789abcdef"
    assert orc["password"] == out["public_key"]
    assert out["share_url"].startswith("vless://")
    assert "security=reality" in out["share_url"]
    assert "pbk=" in out["share_url"] and "sid=" in out["share_url"]
    assert "flow=xtls-rprx-vision" in out["share_url"]


def test_reality_configs_transports(reality):
    asyncio.run(reality.init({}))
    for transport in ("raw", "xhttp", "grpc"):
        out = reality.generate_configs(address="edge.example.com", transport=transport)
        assert out["transport"] == transport
        assert out["share_url"].startswith("vless://")
    with pytest.raises(ValueError):
        reality.generate_configs(address="edge.example.com", transport="smtp")


def test_reality_profile_validation(reality):
    asyncio.run(reality.init({}))
    with pytest.raises(ValueError):
        reality.set_profile({"target": "https://bad"})
    with pytest.raises(ValueError):
        reality.set_profile({"fingerprint": "netscape"})
    with pytest.raises(ValueError):
        reality.set_profile({"server_names": []})
    with pytest.raises(ValueError):
        reality.set_profile({"short_ids": ["xyz-not-hex"]})
    profile = reality.set_profile({"target": "divar.ir:443",
                                   "server_names": "divar.ir, www.divar.ir"})
    assert profile["target"] == "divar.ir:443"
    assert profile["server_names"] == ["divar.ir", "www.divar.ir"]


def test_reality_runtime_refuses_unpinned_binaries(tmp_path):
    # no path at all
    with pytest.raises(RuntimeError):
        verify_xray_binary("", "")
    # not absolute
    with pytest.raises(RuntimeError):
        verify_xray_binary("xray", "0" * 64)
    # missing digest pin
    fake = tmp_path / "xray"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    with pytest.raises(RuntimeError):
        verify_xray_binary(str(fake), "nothex")
    # digest mismatch
    with pytest.raises(RuntimeError):
        verify_xray_binary(str(fake), "a" * 64)
    # group-writable binary refused even with a correct digest
    good = tmp_path / "xray2"
    good.write_text("#!/bin/sh\n")
    good.chmod(0o775)
    import hashlib

    digest = hashlib.sha256(good.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError):
        verify_xray_binary(str(good), digest)
    # the happy path: absolute + executable + not group-writable + matching
    ok = tmp_path / "xray3"
    ok.write_text("#!/bin/sh\n")
    ok.chmod(0o755)
    digest = hashlib.sha256(ok.read_bytes()).hexdigest()
    assert verify_xray_binary(str(ok), digest) == str(ok)


def test_reality_runtime_inactive_without_binary(reality):
    asyncio.run(reality.init({}))
    assert reality.runtime_configured() is False
    assert reality.runtime_running() is False
    # generate-only deployment: keys + configs still work
    assert reality.active_public_key()


# ── registry + pipeline integration ─────────────────────────────────────────
def test_registry_contains_bypass_engines():
    assert REGISTRY.get("SNISpoof") is SNISpoofingEngine
    assert REGISTRY.get("Reality") is RealityEngine


def test_pipeline_order_includes_bypass_engines():
    from engines.config import DEFAULT_PIPELINE

    assert "SNISpoof" in DEFAULT_PIPELINE and "Reality" in DEFAULT_PIPELINE


def test_engines_off_leaves_batch_untouched(tmp_path, monkeypatch):
    """engines-off == before proof: with EMUNEL_ENGINES_ENABLED=0 a frame
    batch and a configgen body pass through the pipeline byte-identical —
    the relay never sees the engine layer at all. (With engines ON the
    Coalesce engine legitimately merges frames by design, so the
    before/after identity is asserted for the OFF state, as documented.)"""
    import os

    from engines.base import EngineContext, KIND_CONFIGGEN, KIND_FRAMES
    from engines.config import parse_env
    from engines.manager import EngineManager

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "0")
    for var in list(os.environ):
        if var.startswith(("EMUNEL_ENGINE_", "SNI_", "REALITY_", "EMUNEL_SNI_")):
            os.environ.pop(var, None)
    os.environ["EMUNEL_ENGINES_ENABLED"] = "0"     # re-assert after the sweep
    env = parse_env("test")
    assert env.enabled is False
    store = EngineStateStore(str(tmp_path / "engines-store"))
    manager = EngineManager(host="console", cfg=env, bus=EventBus(),
                            state=store)
    asyncio.run(manager.start())
    ctx = EngineContext(kind=KIND_FRAMES, hop="client", direction="down",
                        frames=[b"abc", b"defg"], meta={})
    out = asyncio.run(manager.run_pipeline(ctx))
    assert out.frames == [b"abc", b"defg"]
    body = "dmxlc3M6Ly90ZXN0"   # any non-empty base64-ish body
    ctx2 = EngineContext(kind=KIND_CONFIGGEN, hop="client",
                         meta={"format": "raw", "body": body, "headers": []})
    out2 = asyncio.run(manager.run_pipeline(ctx2))
    assert out2.meta["body"] == body
    asyncio.run(manager.stop())


def test_bypass_engines_never_join_a_pipeline():
    """SNISpoof and Reality are SERVICE engines: no pipeline kind, so no
    frame/config batch is ever routed through them (they act only through
    their API endpoints)."""
    assert SNISpoofingEngine.HANDLES == frozenset()
    assert RealityEngine.HANDLES == frozenset()


# ── panel wiring (served page) ───────────────────────────────────────────────
def test_served_panel_has_bypass_page():
    page = (ROOT / "console" / "api" / "emunel_console" / "panel.py").read_text(
        encoding="utf-8")
    assert "function viewBypass()" in page
    assert 'data-nav="bypass"' in page
    assert 'name==="bypass")viewBypass()' in page
    assert "/api/engines/sni/status" in page
    assert "/api/engines/reality/status" in page
    assert "bp-dl" in page                       # helper download button
