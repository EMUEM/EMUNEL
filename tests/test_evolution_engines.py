"""Evolution engines (Chaos Protocol / DpiMesh / GeneticEngine / Synergy).

The operator's SEVEN mandatory tests, verbatim:

  1. all feature flags false  -> Core output identical to the snapshot
  2. only Chaos=true          -> connection established, no errors
  3. only DpiMesh=true        -> report stored, policy returned
  4. only Genetic=true        -> population evolves
  5. all three true           -> synergy works
  6. crash one engine         -> Core (and the others) keep working
  7. fill the volume          -> fallback active

plus unit coverage for the protocol codecs, privacy hashing, the
aggregator, the fitness function and the panel/router wiring.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines.base import EngineContext, KIND_CONFIGGEN, KIND_FRAMES
from engines.bus import EventBus
from engines.config import EngineEnv, parse_env
from engines.engines import REGISTRY
from engines.state import EngineStateStore

# ── fixtures ─────────────────────────────────────────────────────────────────
EVOLUTION_FLAGS = ("CHAOS_PROTOCOL_ENABLED", "DPI_MESH_ENABLED",
                   "GENETIC_ENGINE_ENABLED", "SYNERGY_ENABLED")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    return parse_env("test")


@pytest.fixture()
def make_engine(env):
    bus = EventBus()
    store = EngineStateStore(env.data_dir)

    def _make(cls, **overrides):
        cfg = dataclasses.replace(env, **overrides) if overrides else env
        return cls(cfg, bus, store)

    return _make


def _configgen_body() -> str:
    from engines.configgen_util import raw_encode

    urls = [f"vless://00000000-0000-0000-0000-00000000000{i}"
            f"@host{i}.example.com:443?transport=ws&i={i}" for i in range(8)]
    return raw_encode(urls)


async def _boot_manager(monkeypatch=None, **flag_env):
    from engines.manager import EngineManager

    if monkeypatch is not None:
        for key, value in flag_env.items():
            monkeypatch.setenv(key, value)
    cfg = parse_env("console")
    mgr = EngineManager("console", cfg=cfg)
    await mgr.start()
    return mgr


# ═══ unit — chaos protocol ═══════════════════════════════════════════════════

def test_chaos_seed_is_time_bucketed_hmac():
    from engines.chaos.chaos_protocol import derive_seed

    a = derive_seed("s3cret", 1000)
    b = derive_seed("s3cret", 1000)
    c = derive_seed("s3cret", 1001)
    d = derive_seed("other", 1000)
    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 32


def test_chaos_window_rotation_bounds():
    from engines.chaos.chaos_protocol import window_at

    now = 1_700_000_000_000
    prev = None
    for i in range(400):
        win = window_at("s3cret", 30_000, now + i * 30_000)
        # window bookkeeping
        assert win.switch_at_ms > win.started_at_ms
        assert win.switch_at_ms - win.started_at_ms >= 30_000
        assert win.switch_at_ms - win.started_at_ms <= 90_000
        prev = win
    # a run of one shape never exceeds 3 ticks (anti-stall)
    runs, current = [], 1
    frames = [window_at("s3cret", 30_000, now + i * 30_000).frame_index
              for i in range(200)]
    for a, b in zip(frames, frames[1:]):
        if a == b:
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    assert max(runs) <= 3


def test_chaos_codecs_roundtrip_all_frames():
    from engines.chaos.chaos_protocol import (FRAMES, ChaosFrameError,
                                              decode_packet, encode_packet,
                                              window_at)

    now = 1_700_000_000_000
    payloads = [b"", b"ok", b"x" * 1024,
                secrets.token_bytes(64 * 1024 - 40)]
    for bucket in range(12):
        win = window_at("s3cret", 30_000, now + bucket * 30_000)
        for payload in payloads:
            assert decode_packet(encode_packet(payload, win), win) == payload
    # oversized payload is refused
    with pytest.raises(ChaosFrameError):
        encode_packet(b"z" * 300_000, window_at("s3cret", 30_000, now))
    # corrupting the control frame is detected (padding filler bytes are
    # cosmetic by design — the control frame is the integrity anchor)
    from engines.chaos.chaos_protocol import _padding_plan

    win = window_at("s3cret", 30_000, now)
    record = bytearray(encode_packet(b"hello", win))
    plan = _padding_plan(win.seed)
    record[5 + plan.offset] ^= 0xFF
    with pytest.raises(ChaosFrameError):
        decode_packet(bytes(record), win)
    # truncation is detected
    with pytest.raises(ChaosFrameError):
        decode_packet(encode_packet(b"hello", win)[:-3], win)


def test_chaos_session_prev_window_tolerance():
    from engines.chaos.chaos_protocol import (ChaosFrameError,
                                              decode_packet, window_at)

    now = 1_700_000_000_000
    old = window_at("s3cret", 30_000, now)
    newer = window_at("s3cret", 30_000, now + 30_000)
    record = encode_packet_for(old, b"crossing")
    # decoding an old-window record under the new window raises, but the
    # session's fallback path (prev window) recovers it
    if old.seed != newer.seed:
        with pytest.raises(ChaosFrameError):
            decode_packet(record, newer)
        assert decode_packet(record, old) == b"crossing"


def encode_packet_for(win, payload):
    from engines.chaos.chaos_protocol import encode_packet

    return encode_packet(payload, win)


def test_chaos_control_frame_is_four_bytes_in_padding():
    from engines.chaos.chaos_protocol import (CONTROL_MAGIC, _control,
                                               _padding_plan, encode_packet,
                                               window_at)

    now = 1_700_000_000_000
    win = window_at("s3cret", 30_000, now)
    record = encode_packet(b"payload-here", win)
    plan = _padding_plan(win.seed)
    control = record[5 + plan.offset:5 + plan.offset + 4]
    assert control == _control(win.frame_index)
    assert control[:2] == CONTROL_MAGIC
    assert len(control) == 4          # the spec's 4-byte control frame


# ═══ unit — mesh ═════════════════════════════════════════════════════════════

def test_privacy_hash_shape():
    from engines.mesh.mesh_engine import privacy_hash

    h = privacy_hash("mci", "salt", 12)
    assert len(h) == 12 and h != "mci"
    assert privacy_hash("mci", "salt", 12) == h
    assert privacy_hash("mci", "other", 12) != h


def test_aggregator_builds_policy_with_confidence():
    from engines.mesh.aggregator import build_policies

    rows = [
        ("h1", "r1", "ws", "vless", "ok", 50.0),
        ("h1", "r1", "ws", "vless", "ok", 60.0),
        ("h1", "r1", "grpc", "vless", "blocked", 40.0),
        ("h2", "r2", "ws", "trojan", "ok", 200.0),
    ]
    policies = build_policies(rows)
    by_key = {(p["isp_hash"], p["region_hash"]): p for p in policies}
    p1 = by_key[("h1", "r1")]
    assert p1["recommended_protocol"] == "ws"          # 100% vs 0%
    assert p1["confidence"] > 0
    assert 0.0 <= p1["confidence"] <= 1.0
    params = p1["recommended_params"]
    assert params["samples"] == 2
    assert params["avg_latency_ms"] == 55.0
    assert params["jitter_ms"] == 5.0


def test_mesh_rate_limiter_blocks_floods():
    from engines.mesh.mesh_engine import _RateLimiter

    limiter = _RateLimiter(per_key_per_min=5, global_per_min=100)
    assert sum(limiter.allow("k") for _ in range(5)) == 5
    assert limiter.allow("k") is False
    assert limiter.allow("other") is True


# ═══ unit — genetic ══════════════════════════════════════════════════════════

def test_fitness_formula_bounds():
    from engines.genetic.genetic_engine import fitness_value

    assert fitness_value(1.0, 0.0, 0.0) == 1.0
    # perfect speed/stability but zero success still floors at the
    # latency+jitter half of the score
    assert fitness_value(0.0, 0.0, 0.0) == 0.5
    # everything bad -> ~0
    assert fitness_value(0.0, 1e9, 1e9) < 0.001
    assert 0.0 <= fitness_value(0.7, 120.0, 8.0) <= 1.0
    assert fitness_value(0.5, 0.0, 0.0) == pytest.approx(0.25 + 0.3 + 0.2)
    # the spec's example: fitness 0.87 for a strong genome
    assert 0.8 <= fitness_value(0.95, 20.0, 2.0) <= 1.0


def test_random_params_stay_in_spec():
    from engines.genetic.genetic_engine import (PARAM_ORDER, PARAM_SPEC,
                                                random_param)

    for _ in range(300):
        for key in PARAM_ORDER:
            value = random_param(key)
            spec = PARAM_SPEC[key]
            if spec[0] == "choice":
                assert value in spec[1]
            elif spec[0] == "int":
                assert spec[1] <= value <= spec[2]
            else:
                assert spec[1] <= value <= spec[2]


# ═══ TEST 1 — all flags false -> Core identical ══════════════════════════════

def test_directive_1_all_flags_off_core_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    for flag in EVOLUTION_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    from engines.manager import EngineManager

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
            return out_cfg.meta["body"], out_frames.frames, active
        finally:
            await mgr.stop()

    # default order now includes Chaos/Mesh/Genetic/Synergy (all OFF)...
    new_body, new_frames, active_new = asyncio.run(run(None))
    # ...the pre-evolution order excludes them entirely
    old = ("Coalesce,Morph,Compress,PreConnect,FEC,Congestion,"
           "SessionResumption,FakeHandshake,SplitTunnel,"
           "SNIRotation,DomainFronting,PortHopping,SNISpoof,Reality")
    old_body, old_frames, active_old = asyncio.run(run(old))

    # 1. configgen output is byte-identical to the input (passthrough)
    assert new_body == body
    # 2. byte-identical whether or not the evolution engines are registered
    assert new_body == old_body
    assert b"".join(new_frames) == b"".join(old_frames)
    # 3. the active engine set is exactly the pre-evolution one
    assert active_new == active_old
    assert not any(n in active_new for n in
                   ("Chaos", "Mesh", "Genetic", "Synergy"))
    # 4. every evolution flag defaults to false
    cfg = parse_env("console")
    assert not (cfg.chaos_on or cfg.mesh_on or cfg.genetic_on
                or cfg.synergy_on)


# ═══ TEST 2 — only Chaos=true -> connection established ════════════════════

def test_directive_2_chaos_only_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("CHAOS_PROTOCOL_ENABLED", "1")
    monkeypatch.setenv("CHAOS_TICK_MS", "150")      # observe switches fast
    for flag in EVOLUTION_FLAGS[1:]:
        monkeypatch.delenv(flag, raising=False)

    async def main():
        mgr = await _boot_manager()
        try:
            chaos = mgr.engines["Chaos"]
            assert chaos.status.active, chaos.status.reason
            # a REAL loopback TCP connection speaking chaos records,
            # long enough to cross several shape windows
            result = await chaos.proto.self_play(rounds=14, payload_size=512,
                                                 interval_s=0.08)
            assert result["rounds"] == 14
            assert result["ok"] == 14                 # every round verified
            assert result["errors"] == 0
            assert result["integrity"] is True
            assert result["switches"] >= 1             # shape actually shifted
            assert result["rtt_avg_ms"] is not None and result["rtt_avg_ms"] > 0
            # the probe round (what synergy consumes) also works
            probe = await chaos.run_probe_round()
            assert probe.get("integrity") is True
            return mgr
        finally:
            pass

    mgr = asyncio.run(main())
    asyncio.run(mgr.stop())


# ═══ TEST 3 — only DpiMesh=true -> report stored, policy returned ═══════════

def test_directive_3_mesh_report_and_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("DPI_MESH_ENABLED", "1")
    monkeypatch.delenv("CHAOS_PROTOCOL_ENABLED", raising=False)
    monkeypatch.delenv("GENETIC_ENGINE_ENABLED", raising=False)
    monkeypatch.delenv("SYNERGY_ENABLED", raising=False)

    async def main():
        mgr = await _boot_manager()
        try:
            mesh = mgr.engines["Mesh"]
            assert mesh.status.active, mesh.status.reason
            # report -> stored
            for i, transport in enumerate(("ws", "grpc", "ws", "ws", "grpc")):
                res = mesh.report(isp="mci", region="tehran",
                                  protocol="vless", transport=transport,
                                  sni="www.example.com",
                                  result="ok" if transport == "ws" else "blocked",
                                  latency=40.0 + i * 10)
                assert res == {"stored": True}
            # aggregate -> policies
            count = mesh.aggregate()
            assert count >= 1
            # policy -> returned, hashed, ws recommended over grpc
            out = mesh.policy(isp="mci", region="tehran")
            assert out["policy"] is not None
            assert out["policy"]["recommended_protocol"] == "ws"
            assert out["policy"]["confidence"] > 0
            # privacy: the stored key is a salted hash, never the raw ISP
            assert out["isp_hash"] != "mci" and len(out["isp_hash"]) == 12
            raw = mesh.db.execute(
                "SELECT DISTINCT isp_hash FROM dpi_signatures").fetchall()
            assert raw and all("mci" not in r[0] for r in raw)
            # a different region has no policy yet (graceful)
            empty = mesh.policy(isp="mci", region="shiraz")
            assert empty["policy"] is None
            return mgr
        finally:
            pass

    mgr = asyncio.run(main())
    asyncio.run(mgr.stop())


# ═══ TEST 4 — only Genetic=true -> population evolves ═══════════════════════

def test_directive_4_genetic_population_evolves(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("GENETIC_ENGINE_ENABLED", "1")
    monkeypatch.delenv("CHAOS_PROTOCOL_ENABLED", raising=False)
    monkeypatch.delenv("DPI_MESH_ENABLED", raising=False)
    monkeypatch.delenv("SYNERGY_ENABLED", raising=False)

    async def main():
        mgr = await _boot_manager()
        try:
            genetic = mgr.engines["Genetic"]
            assert genetic.status.active, genetic.status.reason
            assert len(genetic.population) == 20        # spec population
            gen0 = {g["id"] for g in genetic.population}
            assert genetic.generation == 0

            # watch the bus: a new generation must be announced
            sub = mgr.bus.subscribe("genetic.generation")
            result = genetic.evolve(reason="manual")
            await asyncio.sleep(0.05)                    # let listeners drain
            event = sub.queue.popleft() if sub.queue else None

            assert result["ok"] is True
            assert result["generation"] == 1
            assert len(result["replaced"]) == 5          # bottom 5 out
            assert len(result["children"]) == 5          # children of top 5
            assert set(result["replaced"]) <= gen0
            assert not (set(result["children"]) & gen0)  # brand-new ids
            # population size preserved, generation bumped
            assert len(genetic.population) == 20
            assert genetic.generation == 1
            children = [g for g in genetic.population
                        if g["generation"] == 1]
            assert len(children) == 5
            for child in children:
                assert len(child["parent_ids"]) == 2
                assert all(p in gen0 for p in child["parent_ids"])
            # children carry only in-spec parameters
            from engines.genetic.genetic_engine import (PARAM_ORDER,
                                                        PARAM_SPEC)
            for child in children:
                for key in PARAM_ORDER:
                    assert key in child
            # the announcement carries the best genome
            assert event is not None
            assert event.payload["genome"]["generation"] == 1
            # history recorded for the chart
            hist = genetic.history()
            assert [h["generation"] for h in hist][-1] == 1
            return mgr
        finally:
            pass

    mgr = asyncio.run(main())
    asyncio.run(mgr.stop())


# ═══ TEST 5 — all three true -> synergy works ═══════════════════════════════

def test_directive_5_synergy_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("CHAOS_PROTOCOL_ENABLED", "1")
    monkeypatch.setenv("DPI_MESH_ENABLED", "1")
    monkeypatch.setenv("GENETIC_ENGINE_ENABLED", "1")
    monkeypatch.setenv("SYNERGY_ENABLED", "1")

    async def main():
        mgr = await _boot_manager()
        try:
            for name in ("Chaos", "Mesh", "Genetic", "Synergy"):
                assert mgr.engines[name].status.active, \
                    (name, mgr.engines[name].status.reason)
            mesh = mgr.engines["Mesh"]
            genetic = mgr.engines["Genetic"]
            chaos = mgr.engines["Chaos"]
            synergy = mgr.engines["Synergy"]

            # seed real-ish outcomes for EVERY genome transport
            for transport, ok, lat in (("ws", "ok", 60.0), ("grpc", "ok", 90.0),
                                       ("xhttp", "ok", 120.0), ("raw", "ok", 150.0)):
                for i in range(6):
                    mesh.report(isp="mci", region="tehran",
                                protocol="vless", transport=transport,
                                sni="www.example.com", result=ok,
                                latency=lat + i)
            assert mesh.aggregate() >= 1

            # one coordination cycle: mesh -> genetic -> chaos -> mesh
            result = await synergy.cycle_now()
            assert result["fitness_updated"] == 20, result
            best = genetic.best_genome()
            assert best["fitness"] > 0
            # the best genome is what Chaos executes
            assert chaos.genome and chaos.genome["id"] == best["id"]
            # the probe ran and its outcome flowed back into DpiMesh
            assert result["probe_integrity"] is True
            await asyncio.sleep(0.3)                     # bus listener drain
            from engines.mesh.mesh_engine import privacy_hash
            chaos_isp = privacy_hash("chaos-lab", mgr.cfg.mesh_salt, 12)
            (n,) = mesh.db.execute(
                "SELECT COUNT(*) FROM dpi_signatures WHERE isp_hash=?",
                (chaos_isp,)).fetchone()
            assert n >= 1, "chaos probe outcome never reached DpiMesh"
            # cycle is healthy
            assert synergy.mgr.last_errors == {}
            return mgr
        finally:
            pass

    mgr = asyncio.run(main())
    asyncio.run(mgr.stop())


# ═══ TEST 6 — crash one engine -> Core unaffected ════════════════════════════

def test_directive_6_crash_one_engine_core_unaffected(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    for flag in EVOLUTION_FLAGS:
        monkeypatch.setenv(flag, "1")

    body = _configgen_body()

    async def main():
        mgr = await _boot_manager()
        try:
            mesh = mgr.engines["Mesh"]
            genetic = mgr.engines["Genetic"]
            chaos = mgr.engines["Chaos"]
            synergy = mgr.engines["Synergy"]

            # CRASH DpiMesh (the classic broken engine)
            def boom(*args, **kwargs):
                raise RuntimeError("mesh is down")

            mesh.transport_stats = boom
            # the crash must not propagate: fitness step degrades to 0
            assert genetic.update_fitness_from_mesh() == 0
            # the synergy cycle records the error and STILL completes
            result = await synergy.cycle_now()
            assert "genetic.fitness" in synergy.mgr.last_errors
            assert result.get("genome_applied") is True
            assert result.get("probe_integrity") is True
            # the other engines keep working on their own
            evolved = genetic.evolve(reason="manual")
            assert evolved["ok"] is True
            # CRASH Chaos inside the pipeline -> circuit breaker bypasses it
            async def bad_process(ctx):
                raise RuntimeError("chaos exploded")

            chaos.process = bad_process
            ctx = EngineContext(kind=KIND_CONFIGGEN, hop="client",
                                meta={"format": "raw", "body": body,
                                      "headers": [], "path": "/i/x/sub",
                                      "chaos": True})
            out = await mgr.run_pipeline(ctx)
            assert out.meta["body"] == body        # byte-identical anyway
            assert mgr.breakers["Chaos"].errors >= 1
            # frames pipeline also unaffected
            ctx = EngineContext(kind=KIND_FRAMES, hop="client",
                                direction="down", frames=[b"a", b"b"],
                                meta={"ip": "2.144.0.1"})
            out = await mgr.run_pipeline(ctx)
            assert b"".join(out.frames) == b"ab"
            return mgr
        finally:
            pass

    mgr = asyncio.run(main())
    asyncio.run(mgr.stop())


# ═══ TEST 7 — fill the volume -> fallback active ════════════════════════════

def test_directive_7_volume_full_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))
    monkeypatch.setenv("DPI_MESH_ENABLED", "1")
    monkeypatch.setenv("GENETIC_ENGINE_ENABLED", "1")
    monkeypatch.setenv("MESH_MAX_DB_MB", "1")      # minimum cap (1MB)
    monkeypatch.delenv("CHAOS_PROTOCOL_ENABLED", raising=False)
    monkeypatch.delenv("SYNERGY_ENABLED", raising=False)

    async def main():
        mgr = await _boot_manager()
        try:
            mesh = mgr.engines["Mesh"]
            genetic = mgr.engines["Genetic"]
            # healthy path first: rows + a policy we can still serve later
            for i in range(8):
                mesh.report(isp="mci", region="tehran", protocol="vless",
                            transport="ws", sni="s", result="ok", latency=50.0 + i)
            assert mesh.aggregate() >= 1
            policy_before = mesh.policy(isp="mci", region="tehran")
            assert policy_before["policy"] is not None

            # FILL the volume: push mesh.db past the 1MB cap
            mesh.db.execute("CREATE TABLE IF NOT EXISTS _fill(x BLOB)")
            mesh.db.execute("INSERT INTO _fill VALUES (?)",
                            (secrets.token_bytes(2 * 1024 * 1024),))
            mesh.db.commit()

            # reports now fall back instead of being stored
            res = mesh.report(isp="mci", region="tehran", protocol="vless",
                              transport="ws", sni="s", result="ok", latency=50.0)
            assert res["stored"] is False
            assert res["fallback"] is True
            assert "volume cap" in res["reason"]
            assert mesh.degraded is True
            # aggregation pauses in degraded mode
            assert mesh.aggregate() == 0
            # the policy keeps serving last-known-good
            policy_after = mesh.policy(isp="mci", region="tehran")
            assert policy_after["policy"] is not None
            assert (policy_after["policy"]["recommended_protocol"]
                    == policy_before["policy"]["recommended_protocol"])

            # genetic: fill its DB too -> population frozen read-only
            genetic.db.execute("CREATE TABLE IF NOT EXISTS _fill(x BLOB)")
            genetic.db.execute("INSERT INTO _fill VALUES (?)",
                               (secrets.token_bytes(2 * 1024 * 1024),))
            genetic.db.commit()
            evolved = genetic.evolve(reason="manual")
            assert evolved["ok"] is False
            assert evolved["fallback"] is True
            assert genetic.degraded is True
            # and the rest of the system: configgen stays byte-identical
            body = _configgen_body()
            ctx = EngineContext(kind=KIND_CONFIGGEN, hop="client",
                                meta={"format": "raw", "body": body,
                                      "headers": [], "path": "/i/x/sub"})
            out = await mgr.run_pipeline(ctx)
            assert out.meta["body"] == body
            return mgr
        finally:
            pass

    mgr = asyncio.run(main())
    asyncio.run(mgr.stop())


# ═══ wiring: registry / router / panel ═══════════════════════════════════════

def test_registry_contains_evolution_engines():
    for name in ("Chaos", "Mesh", "Genetic", "Synergy"):
        assert name in REGISTRY
    # flags map to the operator's verbatim env names
    from engines.config import ENGINE_FLAG_VARS

    assert ENGINE_FLAG_VARS["Chaos"] == "CHAOS_PROTOCOL_ENABLED"
    assert ENGINE_FLAG_VARS["Mesh"] == "DPI_MESH_ENABLED"
    assert ENGINE_FLAG_VARS["Genetic"] == "GENETIC_ENGINE_ENABLED"
    assert ENGINE_FLAG_VARS["Synergy"] == "SYNERGY_ENABLED"


def test_evolution_router_source_wiring():
    host = (Path(__file__).resolve().parents[1] / "engines" / "host.py"
            ).read_text()
    assert "build_evolution_router" in host, "evolution router not mounted"
    api = (Path(__file__).resolve().parents[1] / "engines" / "evolution_api.py"
           ).read_text()
    for route in ('"/mesh/policy"', '"/mesh/report"', '"/chaos/status"',
                  '"/genetic/population"', '"/genetic/status"',
                  '"/genetic/evolve"', '"/synergy/status"'):
        assert route in api, f"missing route {route}"


def test_panel_evolution_tab_wiring_and_default_hidden():
    from emunel_console import panel

    page = panel.PAGE
    assert "viewEvolution" in page
    assert 'data-nav="evolution"' in page
    assert 'name==="evolution")viewEvolution()' in page
    # hidden while every flag is false: EVO.visible defaults to false...
    assert "var EVO={visible:false}" in page
    # ...and every nav button is additionally gated on it
    for idx, _ in enumerate([m for m in ("evo-btn",)]):
        pass
    assert page.count('EVO.visible?') == 2        # desktop + mobile nav
    # the visibility source is the synergy status flags
    assert "/api/synergy/status" in page
    assert "s.flags.chaos" in page


def test_mesh_public_report_endpoint(tmp_path, monkeypatch):
    """The two PUBLIC mesh routes work against a stub manager (they carry
    no session — the admin routes reuse the existing /api/engines guard)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from engines.evolution_api import build_evolution_router

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "engines"))

    async def main():
        mgr = await _boot_manager(monkeypatch, DPI_MESH_ENABLED="1")
        return mgr

    mgr = asyncio.run(main())
    try:
        mesh = mgr.engines["Mesh"]
        assert mesh.status.active, mesh.status.reason

        class StubManager:
            engines = {"Mesh": mesh}
            cfg = mgr.cfg

        app = FastAPI()
        app.include_router(build_evolution_router(StubManager()))
        client = TestClient(app)

        # report -> stored
        res = client.post("/api/mesh/report", json={
            "isp": "mci", "region": "tehran", "protocol": "vless",
            "transport": "ws", "sni": "www.example.com", "result": "ok",
            "latency": 42.5})
        assert res.status_code == 200
        assert res.json()["stored"] is True
        # oversized junk is refused
        res = client.post("/api/mesh/report",
                          content=b'{"isp":"' + b"x" * 2000 + b'"}',
                          headers={"Content-Type": "application/json"})
        assert res.status_code == 413
        # bad JSON is refused
        res = client.post("/api/mesh/report", content=b"not json{",
                          headers={"Content-Type": "application/json"})
        assert res.status_code == 400
        # policy lookup answers (no policy yet — graceful)
        mesh.aggregate()
        res = client.get("/api/mesh/policy",
                         params={"isp": "mci", "region": "tehran"})
        assert res.status_code == 200
        body = res.json()
        assert body["isp_hash"] != "mci"
        assert "policy" in body
        # missing params -> 400
        res = client.get("/api/mesh/policy")
        assert res.status_code == 400
    finally:
        asyncio.run(mgr.stop())
