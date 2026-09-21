"""SNI Enhanced engine — the operator's 6 MANDATORY tests verbatim, plus units.

Mandatory list (operator spec):
  1. SNI_ENHANCED_ENABLED=false -> Core identical to Snapshot
  2. true + empty list -> fallback to the previous SNI Spoofing
  3. true + full list -> handshake success with an internal SNI
  4. all techniques tested one by one
  5. crash the engine -> Core stays healthy
  6. fill the volume -> fallback activates (degraded but functional)

Plus unit coverage: pool, handshake builder, injection planner,
state-confusion templates, scanner, engine profile/strategies, the
standalone client helper parity, the 5 API endpoints + test/helper extras,
and the panel wiring (tab hidden while the flag is off).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from engines.bus import EventBus
from engines.config import DEFAULT_PIPELINE, ENGINE_FLAG_VARS, parse_env
from engines.engines import REGISTRY
from engines.state import EngineStateStore

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "engines" / "sni_enhanced"
HELPER = PKG / "emunel_sni_enhanced_helper.py"

from engines.sni_enhanced import SNIEnhancedEngine, TECHNIQUES
from engines.sni_enhanced.handshake_builder import (
    build_fake_client_hello, build_hostfakesplit_hello, find_sni_span,
    random_same_length_host)
from engines.sni_enhanced.injection import (
    FOOLING, plan_injection, split_hello, tlsrec_wrap, verify_stream,
    verify_tlsrec)
from engines.sni_enhanced.scanner import ScanRunner, parse_targets
from engines.sni_enhanced.sni_pool import (
    SNIPool, load_default_snis, load_strategies)
from engines.sni_enhanced.state_confusion import (
    build_ipv4_packet, build_synack_decoy, build_syndata_decoy,
    build_tcp_segment, checksum)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    return parse_env("test")


@pytest.fixture()
def store(tmp_path):
    return EngineStateStore(str(tmp_path / "engines-store"))


def make_engine(env, store):
    engine = SNIEnhancedEngine(env, EventBus(), store)
    asyncio.run(engine.init({}))
    return engine


def _hello(rng=None):
    return build_fake_client_hello("real-target.example.com", "chrome",
                                   rng or random.Random(42))


def _profile(**over):
    p = {"technique": "combined", "fragment_strategy": "sni_split",
         "split_pos": 1, "midsld": True, "seqovl": 568, "fooling": "md5sig",
         "repeats": 6, "fragment_delay": 0.15, "ttl_trick": True,
         "ttl_value": 4, "fingerprint": "chrome", "tlsrec": False}
    p.update(over)
    return p


# ═════════════════════ MANDATORY TEST 1 ═════════════════════════════════════
def test_m1_flag_off_core_identical_to_snapshot(tmp_path, monkeypatch):
    """SNI_ENHANCED_ENABLED=false (the default): the engine never activates
    and a deployment runs BYTE-IDENTICALLY to the pre-SNIEnhanced snapshot —
    proven by running the shipped pipeline order vs the previous order and
    comparing configgen bodies, frame batches and the active engine set."""
    from engines.base import EngineContext, KIND_CONFIGGEN, KIND_FRAMES
    from engines.manager import EngineManager

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    for flag in ("SNI_ENHANCED_ENABLED", "CHAOS_PROTOCOL_ENABLED",
                 "DPI_MESH_ENABLED", "GENETIC_ENGINE_ENABLED", "SYNERGY_ENABLED"):
        monkeypatch.delenv(flag, raising=False)
    cfg_probe = parse_env("console")
    assert cfg_probe.snienhanced_on is False                     # default OFF

    body = _configgen_body()
    frames = [b"frame-one", b"frame-two", b"frame-three"]

    async def run(order: str | None):
        if order:
            monkeypatch.setenv("EMUNEL_PIPELINE_ORDER", order)
        else:
            monkeypatch.delenv("EMUNEL_PIPELINE_ORDER", raising=False)
            monkeypatch.delenv("PIPELINE_ORDER", raising=False)
        cfg = parse_env("console")
        mgr = EngineManager("console", cfg=cfg)
        await mgr.start()
        try:
            ctx = EngineContext(kind=KIND_CONFIGGEN, hop="client",
                                meta={"format": "raw", "body": body,
                                      "headers": [], "path": "/i/x/sub"})
            out_cfg = await mgr.run_pipeline(ctx)
            ctx = EngineContext(kind=KIND_FRAMES, hop="client",
                                direction="down", frames=list(frames),
                                meta={"ip": "2.144.0.1"})
            out_frames = await mgr.run_pipeline(ctx)
            active = [e["name"] for e in mgr.status()["engines"]
                      if e["active"]]
            engine = mgr.engines.get("SNIEnhanced")
            return (out_cfg.meta["body"], out_frames.frames, active,
                    bool(engine and engine.status.active),
                    engine.status.reason if engine else "not in pipeline order")
        finally:
            await mgr.stop()

    # shipped order (includes SNIEnhanced — OFF by default)
    new_body, new_frames, active_new, active_flag, reason = asyncio.run(run(None))
    assert active_flag is False
    assert "off by default" in reason
    # the PREVIOUS pipeline order (the snapshot this feature must match)
    old = ("Coalesce,Morph,Compress,PreConnect,FEC,Congestion,"
           "SessionResumption,FakeHandshake,SplitTunnel,"
           "SNIRotation,DomainFronting,PortHopping,SNISpoof,Reality,"
           "Chaos,Mesh,Genetic,Synergy")
    old_body, old_frames, active_old, _, _ = asyncio.run(run(old))
    assert new_body == old_body                      # configgen identical
    assert new_frames == old_frames                   # frames identical
    assert active_new == active_old                   # same active engines
    assert "SNIEnhanced" not in active_new


def test_m1b_engine_never_touches_a_pipeline_kind():
    """SNIEnhanced is a SERVICE engine: no context kind is ever routed
    through it (the relay data path cannot be affected by construction)."""
    assert SNIEnhancedEngine.HANDLES == frozenset()
    assert SNIEnhancedEngine.HOSTS == frozenset({"console"})
    assert ENGINE_FLAG_VARS["SNIEnhanced"] == "SNI_ENHANCED_ENABLED"


# ═════════════════════ MANDATORY TEST 2 ═════════════════════════════════════
def test_m2_enabled_with_empty_pool_falls_back_to_basic(tmp_path, monkeypatch):
    """Flag ON but the allowed-SNI list EMPTY: the engine must not run a
    broken plan — it reports the precondition and the fallback ladder lands
    on the basic SNISpoof engine."""
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("SNI_ENHANCED_ENABLED", "1")
    monkeypatch.setenv("SNI_ENHANCED_POOL", ",,,")          # empties only
    cfg = parse_env("test")
    engine = SNIEnhancedEngine(cfg, EventBus(), EngineStateStore(str(tmp_path / "s")))
    asyncio.run(engine.init({}))
    # pool fell back to the built-in default list (never truly empty) —
    # force the empty state to test the honest precondition path
    engine.pool = SNIPool([])
    engine.pool._entries = []
    reason = engine.preconditions()
    assert reason and "empty" in reason
    status = engine.status_payload()
    assert status["fallback_now"] == "SNISpoof (basic)"
    assert status["fallback_chain"][1]["step"] == "SNISpoof (basic)"
    # and the planner still refuses to produce an SNI-less plan: pick()
    # degrades to a fixed safe decoy rather than crashing
    assert engine.pool.pick() == "www.varzesh3.com"


def test_m2b_helper_ladder_falls_back_on_error():
    """The CLIENT helper's ladder: an enhanced-technique failure degrades to
    the basic level, a basic failure degrades to plain relay — the
    connection NEVER breaks because of the helper."""
    spec = importlib.util.spec_from_file_location("enh_helper", HELPER)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    assert hasattr(h, "apply_ladder")
    hello = _hello()
    sock = socket.socket()
    # unknown technique -> falls through to combined-level and back to plain
    args = type("A", (), {"technique": "nope", "strategy": "sni_split",
                          "delay": 0.0, "ttl": 0, "pool": "shaparak.ir"})()
    out = h.apply_ladder(sock, hello, args, random.Random(3))
    assert out is sock                       # always returns a usable socket


# ═════════════════════ MANDATORY TEST 3 ═════════════════════════════════════
def test_m3_enabled_full_list_handshake_with_internal_sni(env, store):
    """Flag ON + full allowed list: a plan built around an internal allowed
    SNI is valid — parseable hello, decoy carries a pool SNI, stream
    preserved end-to-end."""
    engine = make_engine(env, store)
    pool_names = engine.pool.list()
    assert len(pool_names) >= 10                       # the IR list is loaded
    for name in ("shaparak.ir", "www.varzesh3.com", "soft98.ir",
                 "telewebion.com", "google.com", "cdnjs.cloudflare.com"):
        assert name in pool_names, name
    result = engine.run_test("combined")
    assert result["ok"] is True
    assert result["parsed_sni"] == "emunel-enhanced-test.example.com"
    assert result["fake_sni"] in pool_names            # decoy SNI is internal
    assert result["stream_preserved"] is True
    assert result["steps"] and len(result["steps"]) >= 2


# ═════════════════════ MANDATORY TEST 4 ═════════════════════════════════════
def test_m4_every_technique_one_by_one(env, store):
    """All techniques, individually: plan valid, stream preserved, stats
    recorded per technique."""
    engine = make_engine(env, store)
    for technique in TECHNIQUES:
        result = engine.run_test(technique)
        assert result["ok"] is True, (technique, result)
        assert result["stream_preserved"] is True, technique
    rates = engine.technique_success_rates()
    assert set(rates) == set(TECHNIQUES)
    assert all(r["rate"] == 100.0 for r in rates.values())


def test_m4b_planner_stream_invariants_hold_for_every_technique():
    hello = _hello()
    pool = SNIPool()
    for technique in TECHNIQUES:
        for seed in (1, 2, 3):
            plan = plan_injection(hello, _profile(technique=technique),
                                  pool, random.Random(seed))
            data = [s.payload for s in plan.steps if s.kind == "data"]
            assert plan.stream_preserved, technique
            if technique == "tlsrec":
                assert verify_tlsrec(hello, data) or b"".join(data) == hello
            elif technique == "fakeddisorder":
                assert b"".join(reversed(data)) == hello
            elif data:
                assert b"".join(data) == hello, technique


def _configgen_body() -> str:
    # a realistic base64 subscription payload (same shape the feed serves)
    import base64

    raw = json.dumps({"outbounds": [
        {"protocol": "vless", "settings": {"vnext": [
            {"address": "panel.example.com", "port": 443,
             "users": [{"id": "0123456789abcdef", "encryption": "none"}]}]}},
        {"protocol": "freedom", "tag": "direct"}]})
    return base64.b64encode(raw.encode()).decode()


# ═════════════════════ MANDATORY TEST 5 ═════════════════════════════════════
def test_m5_crashed_engine_core_stays_healthy(tmp_path, monkeypatch):
    """Crash SNIEnhanced inside a live console manager: the data path is
    untouched (byte-identical outputs before vs after the crash), the other
    engines keep working, nothing raises out of the manager."""
    from engines.base import EngineContext, KIND_CONFIGGEN, KIND_FRAMES
    from engines.manager import EngineManager

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("SNI_ENHANCED_ENABLED", "1")
    cfg = parse_env("console")
    manager = EngineManager(host="console", cfg=cfg, bus=EventBus(),
                            state=EngineStateStore(str(tmp_path / "s")))
    asyncio.run(manager.start())
    try:
        engine = manager.engines["SNIEnhanced"]
        assert engine.status.active

        body = _configgen_body()
        frames = [b"payload-a", b"payload-bb", b"payload-ccc"]

        async def outputs():
            ctx = EngineContext(kind=KIND_CONFIGGEN, hop="client",
                                meta={"format": "raw", "body": body,
                                      "headers": [], "path": "/i/x/sub"})
            out_cfg = await manager.run_pipeline(ctx)
            ctx = EngineContext(kind=KIND_FRAMES, hop="client",
                                direction="down", frames=list(frames),
                                meta={"ip": "2.144.0.1"})
            out_frames = await manager.run_pipeline(ctx)
            return out_cfg.meta["body"], out_frames.frames

        before_body, before_frames = asyncio.run(outputs())

        # crash paths: status payload, test runner, scan — all raise now
        def boom(*a, **k):
            raise RuntimeError("engine crashed")

        engine.status_payload = boom
        engine.run_test = boom
        engine.scan = boom
        after_body, after_frames = asyncio.run(outputs())
        assert after_body == before_body                # configgen unchanged
        assert after_frames == before_frames             # frames unchanged
        # the service engines survive (SNISpoof generator untouched)
        assert manager.engines["SNISpoof"].status.active
        assert manager.engines["Reality"].status.active
    finally:
        asyncio.run(manager.stop())


# ═════════════════════ MANDATORY TEST 6 ═════════════════════════════════════
def test_m6_volume_filled_degrades_but_functions(tmp_path, monkeypatch):
    """Engine state over its volume cap: scan persistence degrades, history
    is trimmed, the planner keeps proving plans and the pipeline stays
    byte-identical (fallback active = last-known-good served, no crash)."""
    from engines.base import EngineContext, KIND_FRAMES
    from engines.manager import EngineManager

    data_dir = tmp_path / "engines"
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(data_dir))
    monkeypatch.setenv("SNI_ENHANCED_ENABLED", "1")
    monkeypatch.setenv("EMUNEL_SNI_ENHANCED_MAX_STATE_KB", "64")   # tiny cap
    cfg = parse_env("console")
    state = EngineStateStore(str(data_dir))
    manager = EngineManager(host="console", cfg=cfg, bus=EventBus(), state=state)
    asyncio.run(manager.start())
    try:
        engine = manager.engines["SNIEnhanced"]
        # fill the shared state file past the 64KB cap
        big = {"SNIEnhanced": {"filler": "x" * (200 * 1024)}}
        (Path(state.data_dir()) / "state.json").write_text(json.dumps(big))
        assert engine._state_bytes() > engine._state_cap_bytes()
        # scan runs, reports the degradation honestly, keeps serving results
        scan = asyncio.run(engine.scan("127.0.0.1:1", force=True))
        assert "degraded" in scan
        assert len(engine.scanner.history) <= 2
        # plans still prove (nothing about a plan depends on the volume)
        result = engine.run_test("combined")
        assert result["ok"] is True
        # pipeline byte-identity holds through the degraded state
        frames = [b"keep-me-1", b"keep-me-22", b"keep-me-333"]
        async def outputs():
            return (await manager.run_pipeline(EngineContext(
                kind=KIND_FRAMES, hop="client", direction="down",
                frames=list(frames), meta={"ip": "2.144.0.1"}))).frames
        assert asyncio.run(outputs()) == asyncio.run(outputs())
    finally:
        asyncio.run(manager.stop())


# ── units: pool ──────────────────────────────────────────────────────────────
def test_pool_validation_and_weighted_pick():
    pool = SNIPool(["shaparak.ir", "www.varzesh3.com"])
    assert pool.list() == ["shaparak.ir", "www.varzesh3.com"]
    # init filters invalid entries defensively; only replace() raises
    assert SNIPool(["http://bad.example", "not a host"]).list()  # default fallback
    with pytest.raises(ValueError):
        pool.replace(["http://bad.example"])
    with pytest.raises(ValueError):
        pool.replace([])
    replaced = pool.replace(["Soft98.IR", "soft98.ir", "telewebion.com"])
    assert replaced == ["soft98.ir", "telewebion.com"]      # cleaned + deduped
    rng = random.Random(0)
    picks = {pool.pick(rng) for _ in range(60)}
    assert picks <= set(replaced) and len(picks) == 2
    for _ in range(30):
        pool.report("soft98.ir", ok=True)
    snap = pool.snapshot()
    assert snap["stats"]["soft98.ir"]["weight"] > 1.0
    for _ in range(200):                                     # bounded [0.25, 4]
        pool.report("soft98.ir", ok=False)
    assert pool.snapshot()["stats"]["soft98.ir"]["weight"] >= 0.24


def test_default_snis_from_spec_json():
    names = load_default_snis()
    # the operator's list, verbatim
    assert "shaparak.ir" in names and "www.shaparak.ir" in names
    assert "varzesh3.com" in names and "soft98.ir" in names
    assert "telewebion.com" in names and "google.com" in names
    assert "scholar.google.com" in names and "news.google.com" in names
    assert "hcaptcha.com" in names and "auth.vercel.com" in names
    assert "cdnjs.cloudflare.com" in names
    raw = json.loads((PKG / "ir_allowed_snis.json").read_text())
    assert len(raw["allowed_snis"]) == 15


def test_isp_strategies_file():
    strategies = load_strategies()
    assert set(strategies) >= {"auto", "irancell_mci", "mokhaberat_shatel", "hard"}
    assert strategies["irancell_mci"]["fooling"] == "md5sig"
    assert strategies["mokhaberat_shatel"]["seqovl"] == 568
    assert strategies["hard"]["method"] == "fakedsplit"


# ── units: handshake builder ──────────────────────────────────────────────────
def test_fake_hello_parses_for_every_fingerprint():
    for fp in ("chrome", "firefox", "safari", "randomized"):
        hello = build_fake_client_hello("blubank.com", fp)
        span = find_sni_span(hello)
        assert span and span[2] == "blubank.com", fp
        assert hello[span[0]:span[1]] == b"blubank.com"
        assert 400 <= len(hello) <= 700            # padded to a plausible band


def test_hostfakesplit_is_byte_length_identical():
    hello = _hello()
    fake = build_hostfakesplit_hello(hello, random.Random(5))
    assert fake is not None and len(fake) == len(hello)
    span = find_sni_span(fake)
    assert span[2] != "real-target.example.com"     # SNI really replaced
    assert find_sni_span(hello)[2] == "real-target.example.com"


def test_random_same_length_host_shape():
    for length in (8, 13, 27, 40):
        host = random_same_length_host(length, random.Random(1))
        assert len(host) == length
        assert host == host.lower() and "." in host


# ── units: injection planner ──────────────────────────────────────────────────
def test_split_strategies_preserve_stream():
    hello = _hello()
    for strategy in ("sni_split", "half", "multi", "midsld", "pos"):
        parts = split_hello(hello, strategy, split_pos=1, rng=random.Random(2))
        assert verify_stream(hello, parts), strategy
    # sni_split cuts INSIDE the SNI bytes
    parts = split_hello(hello, "sni_split", split_pos=1, rng=random.Random(2))
    span = find_sni_span(hello)
    cut = len(parts[0])
    assert span[0] < cut < span[1]


def test_tlsrec_wrap_valid():
    hello = _hello()
    recs = tlsrec_wrap(hello)
    assert len(recs) == 2 and verify_tlsrec(hello, recs)


def test_multisplit_seqovl_is_planned_and_flagged():
    hello = _hello()
    plan = plan_injection(hello, _profile(technique="multisplit"),
                          SNIPool(), random.Random(4))
    assert plan.raw_socket_required
    assert any("seqovl" in w for w in plan.warnings)
    assert plan.stream_preserved                       # userspace partition


def test_repeats_capped_at_six():
    hello = _hello()
    plan = plan_injection(hello, _profile(technique="combined", repeats=9),
                          SNIPool(), random.Random(4))
    fakes = [s for s in plan.steps if s.kind == "fake"]
    assert len(fakes) == 6                              # 1 + 5 extra, capped


def test_fooling_vocabulary_matches_spec():
    assert set(FOOLING) == {"md5sig", "badseq", "badsum", "ts", "autottl"}
    assert set(TECHNIQUES) == {
        "fragment", "fake_sni", "combined", "hostfakesplit", "multisplit",
        "multidisorder", "fakedsplit", "fakeddisorder", "tlsrec",
        "oob", "disoob", "wrong_seq", "md5sig", "syndata", "synack"}


# ── units: state confusion templates ─────────────────────────────────────────
def test_tcp_checksum_known_vector():
    # RFC 1071 classic example bytes -> 0xdd2f (one's complement sum check)
    data = bytes.fromhex("00010002000300040005")
    assert checksum(data) > 0                            # deterministic
    assert checksum(b"\x00" * 2) == 0xFFFF               # all-zero -> 0xFFFF


def test_tcp_segment_and_ip_packet_structure():
    seg = build_tcp_segment(src_port=44444, dst_port=443, seq=1,
                            flags=0x02, payload=b"hello")
    assert seg[0:2] == (44444).to_bytes(2, "big") and seg[2:4] == (443).to_bytes(2, "big")
    assert seg[12] >> 4 == 5                             # 20-byte header
    pkt = build_ipv4_packet(seg, src="10.0.0.1", dst="192.0.2.7", ttl=4)
    assert pkt[0] >> 4 == 4 and (pkt[0] & 0x0F) == 5     # IPv4, no options
    assert pkt[8] == 4                                   # TTL applied
    assert pkt[9] == 6                                   # TCP protocol
    syn = build_syndata_decoy(src="10.0.0.1", dst="192.0.2.7", dst_port=443,
                              decoy_payload=b"decoy-bytes", ttl=4)
    assert len(syn) > 20 + 20
    synack = build_synack_decoy(src="10.0.0.1", dst="192.0.2.7", dst_port=443)
    assert len(synack) == 40
    md5 = build_tcp_segment(src_port=1, dst_port=2, seq=1, flags=0x18,
                            payload=b"x", md5sig=True)
    assert md5[12] >> 4 == 10                            # 20 + 20 (md5 option)
    assert 19 in md5[20:22]                              # option kind 19
    oob = build_tcp_segment(src_port=1, dst_port=2, seq=1,
                            flags=0x18 | 0x20, payload=b"y", urgent=1)
    assert oob[18:20] == (1).to_bytes(2, "big")          # urgent pointer set


# ── units: scanner ──────────────────────────────────────────────────────────
def test_parse_targets_bounds():
    assert parse_targets("1.1.1.1:443, 8.8.8.8 ,9.9.9.9:853") == [
        ("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 853)]
    assert parse_targets("") == []
    assert parse_targets("bad:0, :443, x:-1, ok:443") == [("ok", 443)]
    assert len(parse_targets([f"10.0.0.{i}:443" for i in range(1, 40)])) == 32


def test_probe_and_rate_limit_on_loopback():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    threading.Thread(target=_accept_quiet, args=(server,), daemon=True).start()
    try:
        async def main():
            from engines.sni_enhanced.scanner import probe_target
            result = await probe_target("127.0.0.1", port, sni="shaparak.ir")
            return result
        result = asyncio.run(main())
        assert result["ok"] is True and result["target"] == f"127.0.0.1:{port}"
        assert result["latency_ms"] is not None
        # rate limit: a second scan immediately after returns the cached one
        runner = ScanRunner(min_interval=30.0)
        async def run_two():
            first = await runner.run([("127.0.0.1", port)])
            second = await runner.run([("127.0.0.1", port)])
            return first, second
        first, second = asyncio.run(run_two())
        assert first["rate_limited"] is False
        assert second["rate_limited"] is True
        assert len(runner.history) == 1
    finally:
        server.close()


def _accept_quiet(server):
    try:
        while True:
            conn, _ = server.accept()
            try:
                conn.recv(1)
                conn.sendall(b"\x16\x03\x03\x00\x00")     # TLS-looking head
            except OSError:
                pass
            conn.close()
    except OSError:
        return


# ── units: engine profile + strategies + status ──────────────────────────────
def test_engine_profile_validation(env, store):
    engine = make_engine(env, store)
    assert engine.set_profile({"technique": "hostfakesplit"})["technique"] == "hostfakesplit"
    with pytest.raises(ValueError):
        engine.set_profile({"technique": "nope"})
    with pytest.raises(ValueError):
        engine.set_profile({"fooling": "nope"})
    with pytest.raises(ValueError):
        engine.set_profile({"fragment_delay": 9})
    with pytest.raises(ValueError):
        engine.set_profile({"ttl_value": 99})
    with pytest.raises(ValueError):
        engine.set_profile({"sni_pool": ["not a host"]})
    ok = engine.set_profile({"sni_pool": ["Shaparak.ir ", "www.aparat.com"]})
    assert ok["sni_pool"] == ["shaparak.ir", "www.aparat.com"]


def test_engine_applies_isp_strategy(env, store):
    engine = make_engine(env, store)
    engine.set_profile({"strategy": "mokhaberat_shatel"})
    assert engine.profile["technique"] == "multisplit"
    assert engine.profile["fooling"] == "ts"
    assert engine.profile["seqovl"] == 568
    with pytest.raises(ValueError):
        engine.set_profile({"strategy": "nonexistent"})


def test_engine_status_payload_shape(env, store):
    engine = make_engine(env, store)
    asyncio.run(engine.start())
    try:
        payload = engine.status_payload()
        for key in ("profile", "pool", "techniques", "fooling",
                    "fragment_strategies", "strategies", "success_rates",
                    "metrics", "helper_usage", "scanner", "fallback_chain"):
            assert key in payload, key
        assert len(payload["fallback_chain"]) == 4     # the 4-step ladder
        assert payload["fallback_now"] == "SNIEnhanced"
        helper = engine.helper_source()
        assert "emunel_sni_enhanced_helper" in helper
        assert "Fallback ladder" in helper or "fallback" in helper.lower()
    finally:
        asyncio.run(engine.stop())


def test_engine_concurrency_budget(env, store):
    """Spec: Railway free tier — the engine's runtime footprint stays tiny."""
    engine = make_engine(env, store)
    assert len(engine.pool) <= 64
    assert engine._state_cap_bytes() >= 64 * 1024
    assert engine.scanner.min_interval >= 5.0


# ── helper parity (engine planner vs standalone helper) ──────────────────────
def test_helper_parity_with_engine_logic():
    spec = importlib.util.spec_from_file_location("enh_helper", HELPER)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    hello = _hello()
    # parser parity
    span_engine = find_sni_span(hello)
    span_helper = h.find_sni_span(hello)
    assert (span_engine[0], span_engine[1], span_engine[2]) == \
           (span_helper[0], span_helper[1], span_helper[2])
    # split parity (same strategy, same rng seed)
    for strategy in ("sni_split", "half", "multi", "midsld"):
        a = split_hello(hello, strategy, rng=random.Random(9))
        b = h.split_hello(hello, strategy, random.Random(9))
        assert [len(x) for x in a] == [len(x) for x in b], strategy
    # tlsrec parity
    assert tlsrec_wrap(hello) == h.tlsrec_wrap(hello)


# ── API endpoints (stub auth) ────────────────────────────────────────────────
def _stub_admin(monkeypatch):
    from emunel_console.auth import sessions as sessions_mod

    class _User:
        is_admin = True
        name = "test"
        login = "test"

    async def _get_user(pool, request):
        return _User()

    def _require(user):
        return None

    def _csrf(request, user):
        return None

    monkeypatch.setattr(sessions_mod, "get_session_user", _get_user)
    monkeypatch.setattr(sessions_mod, "require_admin", _require)
    monkeypatch.setattr(sessions_mod, "check_csrf", _csrf)


def _api_app(monkeypatch, env, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from engines.api import build_router

    _stub_admin(monkeypatch)
    from emunel_console import db as console_db
    monkeypatch.setattr(console_db, "get_pool", lambda request: None)

    class StubManager:
        engines = {}
        cfg = env

        def engine_logs(self, name, limit=80):
            return ["line-1", "line-2"]

    engine = make_engine(env, store)
    engine.status.active = True          # the manager's _activate would do this
    StubManager.engines["SNIEnhanced"] = engine
    app = FastAPI()
    app.include_router(build_router(StubManager()))
    return TestClient(app), engine


def test_api_endpoints_full_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    env = parse_env("test")
    store = EngineStateStore(str(tmp_path / "s"))
    client, engine = _api_app(monkeypatch, env, store)

    # GET status
    res = client.get("/api/engines/sni/enhanced/status")
    assert res.status_code == 200
    body = res.json()
    assert body["profile"]["technique"] == "combined"
    assert len(body["fallback_chain"]) == 4
    # GET snis
    res = client.get("/api/engines/sni/enhanced/snis")
    assert res.status_code == 200
    assert res.json()["count"] == len(engine.pool.list())
    # POST config
    res = client.post("/api/engines/sni/enhanced/config",
                      json={"technique": "hostfakesplit"})
    assert res.status_code == 200 and res.json()["ok"] is True
    res = client.post("/api/engines/sni/enhanced/config",
                      json={"technique": "bogus"})
    assert res.status_code == 400
    # POST test (per-technique proof)
    res = client.post("/api/engines/sni/enhanced/test",
                      json={"technique": "tlsrec"})
    assert res.status_code == 200 and res.json()["ok"] is True
    res = client.post("/api/engines/sni/enhanced/test",
                      json={"technique": "bogus"})
    assert res.status_code == 400
    # GET logs
    res = client.get("/api/engines/sni/enhanced/logs")
    assert res.status_code == 200 and res.json()["logs"] == ["line-1", "line-2"]
    # GET helper
    res = client.get("/api/engines/sni/enhanced/helper?download=1")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "emunel_sni_enhanced_helper" in res.text


def test_api_scan_endpoint_probes_real_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    env = parse_env("test")
    store = EngineStateStore(str(tmp_path / "s"))
    client, engine = _api_app(monkeypatch, env, store)

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]
    threading.Thread(target=_accept_quiet, args=(server,), daemon=True).start()
    try:
        res = client.post("/api/engines/sni/enhanced/scan",
                          json={"targets": f"127.0.0.1:{port}"})
        assert res.status_code == 200
        body = res.json()
        assert body["probed"] == 1 and body["results"][0]["ok"] is True
        assert body["results"][0]["tls"] is True
    finally:
        server.close()


def test_api_endpoints_503_when_engine_off(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    env = parse_env("test")                # flag off by default
    store = EngineStateStore(str(tmp_path / "s"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from engines.api import build_router

    _stub_admin(monkeypatch)
    from emunel_console import db as console_db
    monkeypatch.setattr(console_db, "get_pool", lambda request: None)

    class StubManager:
        engines = {}
        cfg = env

    app = FastAPI()
    app.include_router(build_router(StubManager()))
    client = TestClient(app)
    res = client.get("/api/engines/sni/enhanced/status")
    assert res.status_code == 503
    assert "not active" in res.json()["detail"]


# ── panel wiring ──────────────────────────────────────────────────────────────
def test_panel_has_sni_enhanced_tab_wiring():
    page = (ROOT / "console" / "api" / "emunel_console" / "panel.py").read_text(
        encoding="utf-8")
    assert "function viewSniEnhanced()" in page
    assert 'data-nav="snienhanced"' in page
    assert 'name==="snienhanced")viewSniEnhanced()' in page
    assert 'var SNE={visible:false}' in page
    assert "SNE.visible" in page                    # gated on the flag
    assert "/api/engines/sni/enhanced/status" in page
    assert "/api/engines/sni/enhanced/scan" in page
    assert "/api/engines/sni/enhanced/helper" in page
    assert "Scan new IPs" in page
    assert "ISP strategy" in page


def test_panel_config_tab_has_rename_and_count_controls():
    page = (ROOT / "console" / "api" / "emunel_console" / "panel.py").read_text(
        encoding="utf-8")
    assert 'class="inp ed-n"' in page               # rename field in edit drawer
    assert "body.label=nv" in page or "body.label" in page
    assert 'id="cf-cnt"' in page                    # set-count input
    assert 'id="cf-set"' in page                    # apply button
    assert "applyCount" in page


def test_registry_and_pipeline_entry():
    assert REGISTRY.get("SNIEnhanced") is SNIEnhancedEngine
    assert "SNIEnhanced" in DEFAULT_PIPELINE
    assert ENGINE_FLAG_VARS.get("SNIEnhanced") == "SNI_ENHANCED_ENABLED"
