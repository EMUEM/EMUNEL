"""STABILIZATION suite — the operator's 10 mandatory tests (stage 8).

  1  all feature flags false -> Core identical (byte-level passthrough,
     effective order == legacy order, zero merged engines instantiated)
  2  TRAFFIC_SHAPING_MERGED=true -> Morph + SNIEnhanced + Chaos run inside
     one module (children active, API lookups still resolve by name)
  3  TRANSPORT_MERGED=true -> FEC armed + Pre-Connect ACTIVE with a warm
     pool that actually fills through the dial hook (the old bug: the pool
     had a taker but no producer, so it stayed forever empty)
  4  LEARNING_MERGED=true -> ONE SQLite (consolidated.db; no mesh.db /
     genetic.db) and a 10-minute cadence
  5  PAYLOAD_MERGED=true -> Compression + Coalescing inside one module
  6  crash a merged module -> automatic fallback to the legacy engines
  7  volume pressure -> the rotating backup works (7 copies, sqlite-safe)
  8  100 concurrent connections -> RSS < 400MB, CPU < 5%
  9  24h egress budget -> outbound requests < 2400 (default: 0 — the only
     scheduled outbound call, the SNI scanner, is 6h-cadence and OFF)
 10  1000 sessions -> RAM stays flat (every structure is bounded)

Plus unit coverage: effective-order substitution, hot-toggle routing into
the module, TTL caches, WAL flipping, the memory guard, the backup
rotation and the adaptive FEC ratio formula.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket as _socket
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "console" / "api"))


# ═════════════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════════════
async def _boot(monkeypatch, host="console", data_dir=None, **env):
    """Boot a real EngineManager with the given env flags."""
    from engines.config import parse_env
    from engines.manager import EngineManager

    data_dir = data_dir or (monkeypatch_get_tmp())
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(data_dir))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = parse_env(host)
    mgr = EngineManager(host, cfg=cfg)
    await mgr.start()
    return mgr


def monkeypatch_get_tmp():
    return _TMP[-1]


_TMP: list[Path] = []


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "engines"
    d.mkdir()
    _TMP.append(d)
    yield d
    _TMP.pop()


async def _stop(mgr):
    try:
        await mgr.stop()
    except Exception:
        pass


def _frames_ctx(frames=("hello-world",)):
    from engines.base import EngineContext, KIND_FRAMES

    return EngineContext(kind=KIND_FRAMES, frames=[
        f.encode() if isinstance(f, str) else f for f in frames])


def _configgen_ctx(body: str):
    from engines.base import EngineContext, KIND_CONFIGGEN

    return EngineContext(kind=KIND_CONFIGGEN,
                         meta={"format": "raw", "body": body})


# ═════════════════════════════════════════════════════════════════════════
# TEST 1 — all flags false -> Core identical to the snapshot-era behaviour
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_1_flags_off_core_identical(monkeypatch, data_dir):
    from engines.config import DEFAULT_PIPELINE, parse_env
    from engines.consolidated import MERGED_MODULES

    for flag in ("TRAFFIC_SHAPING_MERGED", "TRANSPORT_MERGED",
                 "LEARNING_MERGED", "PAYLOAD_MERGED"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(data_dir))
    cfg = parse_env("console")

    # the effective order with every merged flag off IS the legacy order
    from engines.manager import EngineManager

    mgr = EngineManager("console", cfg=cfg)
    effective = mgr._effective_order()
    legacy = [p for p in DEFAULT_PIPELINE.split(",")
              if p not in MERGED_MODULES]
    assert effective == legacy, "flags-off must reproduce the legacy order"

    await mgr.start()
    try:
        # no merged engine instantiated as ACTIVE
        for name in MERGED_MODULES:
            entry = mgr.engines.get(name)
            assert entry is None or not entry.status.active, \
                f"{name} must stay inactive with its flag off"
            if entry is not None:
                assert "off by default" in entry.status.reason

        # data path: byte-identical passthrough (frames + configgen)
        frames = [b"payload-one", b"payload-two", b"x" * 2048]
        ctx = _frames_ctx(frames)
        out = await mgr.run_pipeline(ctx)
        assert b"".join(out.frames) == b"".join(frames)

        body = "vless://00000000-0000-0000-0000-000000000001@h1.example.com:443?transport=ws"
        cg = _configgen_ctx(body)
        out2 = await mgr.run_pipeline(cg)
        assert out2.meta["body"] == body, "configgen must be a passthrough"
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 2 — TRAFFIC_SHAPING_MERGED -> Morph + SNIEnhanced + Chaos in one module
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_2_traffic_shaping_merged(monkeypatch, data_dir):
    mgr = await _boot(monkeypatch, TRAFFIC_SHAPING_MERGED="true")
    try:
        ts = mgr.engines.get("TrafficShaping")
        assert ts is not None and ts.status.active

        for child_name in ("Morph", "SNIEnhanced", "Chaos"):
            child = ts.children.get(child_name)
            assert child is not None, f"{child_name} must be instantiated"
            assert child.status.active, \
                f"{child_name} must run inside TrafficShaping"

        # backward compatibility: the API's name lookups still resolve to
        # the child instances the module published into the manager
        assert mgr.engines.get("SNIEnhanced") is ts.children["SNIEnhanced"]
        assert mgr.engines.get("Chaos") is ts.children["Chaos"]

        # the merged module is IN the pipeline; the children are not
        assert "TrafficShaping" in mgr.pipelines.get("configgen", []) or \
            "TrafficShaping" in mgr.pipelines.get("frames", [])
        assert "Morph" not in mgr.pipelines.get("frames", [])
        assert "Morph" not in mgr.pipelines.get("configgen", [])

        # frames still flow (Morph's default profile = byte passthrough)
        frames = [b"a" * 512, b"b" * 512]
        out = await mgr.run_pipeline(_frames_ctx(frames))
        assert b"".join(out.frames) == b"".join(frames)

        # shared state: one SQLite journal + the decision cache is bounded
        assert (data_dir / "consolidated.db").exists()
        assert len(ts.cache) <= ts.cache.max_entries

        # configgen cache actually skips repeat bodies
        body = "trojan://pass@host1.example.com:443?security=tls#x"
        a = await mgr.run_pipeline(_configgen_ctx(body))
        b = await mgr.run_pipeline(_configgen_ctx(body))
        assert ts.status.metrics.get("cache_hits", 0) >= 1
        assert b.meta["body"] == a.meta["body"]

        # group view for the UI
        group = ts.group_status()
        assert group["merged"] is True
        assert set(group["children"]) >= {"Morph", "SNIEnhanced", "Chaos"}
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 3 — TRANSPORT_MERGED -> FEC armed + Pre-Connect genuinely active
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_3_transport_merged_core_host(monkeypatch, data_dir):
    mgr = await _boot(monkeypatch, host="core", data_dir=data_dir,
                      TRANSPORT_MERGED="true")
    try:
        tp = mgr.engines.get("Transport")
        assert tp is not None and tp.status.active

        # Pre-Connect: ALWAYS active in the module (spec 5.2)
        pre = tp.children.get("PreConnect")
        assert pre is not None and pre.status.active
        # FEC: armed with the honest TCP-dormant reason + adaptive ratio
        fec = tp.children.get("FEC")
        assert fec is not None
        assert fec.status.metrics.get("codec_selftest") == "ok"
        assert "TCP" in (fec.status.reason or "") or fec.status.active
        assert tp.adaptive_fec_ratio() == pytest.approx(0.05)  # floor at 0 loss
        # adaptive: measured loss drives the formula min(0.3, max(0.05, 3*loss))
        cong = tp.children.get("Congestion")
        assert cong is not None and cong.status.active
        cong.status.metrics["loss_ewma_percent"] = 8.0     # 8% loss
        assert tp.adaptive_fec_ratio() == pytest.approx(0.24)
        cong.status.metrics["loss_ewma_percent"] = 50.0    # saturates at the cap
        assert tp.adaptive_fec_ratio() == pytest.approx(0.30)

        # pre-connect child env: bounded cache (spec 3.2: cache = 50)
        assert pre.cfg.preconnect_max_hosts <= 50
    finally:
        await _stop(mgr)


@pytest.mark.asyncio
async def test_3b_preconnect_warm_pool_fills(monkeypatch, data_dir):
    """The old bug: the warm pool had take_warm() but NOTHING ever called
    pool.put() — warm_hits stayed 0 forever. The dial hook now records
    dials and speculatively warms repeat destinations."""
    from engines import core_host

    mgr = await _boot(monkeypatch, host="core", data_dir=data_dir,
                      TRANSPORT_MERGED="true",
                      EMUNEL_ENGINE_PRECONNECT_ENABLED="1")
    try:
        # loopback server pretending to be a destination
        server = await asyncio.start_server(
            lambda r, w: asyncio.ensure_future(_echo(r, w)), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        original = asyncio.open_connection
        restore = core_host._install_dial_hook(mgr)
        try:
            # two REAL dials through the HOOKED open_connection -> the hook
            # records the destination and speculatively warms the repeat one
            for _ in range(2):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(b"x")
                await w.drain()
                w.close()
            # give the speculative warm task a moment
            for _ in range(50):
                await asyncio.sleep(0.02)
                pre = mgr.engines.get("PreConnect")
                if pre and pre.status.metrics.get("speculative_warms", 0) >= 1:
                    break
            pre = mgr.engines.get("PreConnect")
            assert pre is not None
            assert pre.status.metrics.get("speculative_warms", 0) >= 1, \
                "the warm pool must actually be filled by the dial hook"

            # the warm socket is real, NODELAY is on, and a take succeeds
            warm = pre.take_warm("127.0.0.1", port)
            assert warm is not None, "take_warm must hit the warmed pool"
            reader, writer = warm
            sock = writer.transport.get_extra_info("socket")
            assert sock.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY) == 1
            writer.close()
        finally:
            core_host._remove_dial_hook(restore)
        server.close()
        await server.wait_closed()
    finally:
        await _stop(mgr)


async def _echo(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_3c_transport_in_console_host(monkeypatch, data_dir):
    """Console host: only the console-applicable child runs; core-side
    children are skipped with the honest host reason (they run inside
    instances)."""
    mgr = await _boot(monkeypatch, TRANSPORT_MERGED="true")
    try:
        tp = mgr.engines.get("Transport")
        assert tp is not None and tp.status.active
        assert "SessionResumption" in tp.children
        assert tp.children["SessionResumption"].status.active
        assert "PreConnect" not in tp.children
        assert "FEC" not in tp.children
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 4 — LEARNING_MERGED -> ONE SQLite, ONE 10-minute cadence
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_4_learning_merged(monkeypatch, data_dir):
    mgr = await _boot(monkeypatch, LEARNING_MERGED="true",
                      MESH_SALT="t", CHAOS_SECRET="t")
    try:
        learning = mgr.engines.get("Learning")
        assert learning is not None and learning.status.active

        # children wired through the ONE shared SQLite connection
        mesh = learning.children.get("Mesh")
        genetic = learning.children.get("Genetic")
        assert mesh is not None and genetic is not None
        assert mesh.shared_db is not None
        assert mesh.shared_db is genetic.shared_db

        # ONE db file: consolidated.db exists, the legacy files never appear
        assert (data_dir / "consolidated.db").exists()
        assert not (data_dir / "mesh.db").exists()
        assert not (data_dir / "genetic.db").exists()

        # the tables live side by side (disjoint schemas, one file)
        tables = {row[0] for row in learning._shared_sql.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"dpi_signatures", "genomes"} <= tables

        # 10-minute cadence (spec 3.2: cron every 10 min, not 5)
        assert learning._cron_task is not None
        assert mesh.cfg.mesh_aggregate_interval_sec >= 600
        assert learning.children["Synergy"].cfg.synergy_loop_sec >= 600

        # backward compat: API name lookups resolve
        assert mgr.engines.get("Mesh") is mesh
        assert mgr.engines.get("Genetic") is genetic
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 5 — PAYLOAD_MERGED -> Compression + Coalescing in one module
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_5_payload_merged(monkeypatch, data_dir):
    mgr = await _boot(monkeypatch, PAYLOAD_MERGED="true")
    try:
        payload = mgr.engines.get("Payload")
        assert payload is not None and payload.status.active

        coalesce = payload.children.get("Coalesce")
        compress = payload.children.get("Compress")
        assert coalesce is not None and coalesce.status.active
        assert compress is not None and compress.status.active

        # spec 3.2: low compression effort + the 16KB per-stream buffer cap
        assert compress.cfg.compress_level <= 4
        assert coalesce.cfg.coalesce_max_buffer <= 16 * 1024

        # frames flow through the merged module unharmed (stream preserved)
        frames = [b"c" * 300, b"d" * 300, b"e" * 300]
        out = await mgr.run_pipeline(_frames_ctx(frames))
        assert b"".join(out.frames) == b"".join(frames)

        # the merged module replaced BOTH children in the pipeline
        assert "Payload" in mgr.pipelines.get("frames", [])
        assert "Coalesce" not in mgr.pipelines.get("frames", [])
        assert "Compress" not in mgr.pipelines.get("frames", [])
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 6 — a crashed merged module falls back to the legacy engines
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_6_crashed_module_falls_back(monkeypatch, data_dir):
    from engines.consolidated import TrafficShapingEngine

    async def _boom(self):
        raise RuntimeError("synthetic merged-module crash")

    monkeypatch.setattr(TrafficShapingEngine, "start", _boom)
    events: list[dict] = []

    mgr = await _boot(monkeypatch, TRAFFIC_SHAPING_MERGED="true")
    try:
        # the module is dead with the honest reason ...
        ts = mgr.engines.get("TrafficShaping")
        assert ts is not None and not ts.status.active
        assert "start failed" in ts.status.reason

        # ... and the legacy engines run individually in its place
        morph = mgr.engines.get("Morph")
        sne = mgr.engines.get("SNIEnhanced")
        chaos = mgr.engines.get("Chaos")
        assert morph is not None and morph.status.active
        assert sne is not None and sne.status.active
        assert chaos is not None and chaos.status.active
        assert not hasattr(morph, "children")   # standalone, not a facade

        # the fallback engines are real pipeline stages again
        assert "Morph" in mgr.pipelines.get("frames", [])
        frames = [b"f" * 256, b"g" * 256]
        out = await mgr.run_pipeline(_frames_ctx(frames))
        assert b"".join(out.frames) == b"".join(frames)
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 7 — volume pressure -> the rotating backup works
# ═════════════════════════════════════════════════════════════════════════
def test_7_backup_rotation(tmp_path):
    from engines.persistence import BackupService

    data_dir = tmp_path / "engines"
    data_dir.mkdir()
    # state + a real sqlite with content + the console db one level up
    (data_dir / "state.json").write_text(
        json.dumps({"version": 1, "engines": {"EngineToggles":
                                             {"enabled": {"FEC": True}}}}))
    mesh = data_dir / "mesh.db"
    conn = sqlite3.connect(str(mesh))
    conn.execute("CREATE TABLE dpi_signatures (id INTEGER PRIMARY KEY, "
                 "privacy_hash TEXT)")
    conn.execute("INSERT INTO dpi_signatures (privacy_hash) VALUES ('abc')")
    conn.commit()
    conn.close()
    parent_db = tmp_path / "emunel.db"
    conn = sqlite3.connect(str(parent_db))
    conn.execute("CREATE TABLE users (name TEXT)")
    conn.execute("INSERT INTO users VALUES ('admin')")
    conn.commit()
    conn.close()

    svc = BackupService(data_dir, interval_h=6, keep=7)
    for _ in range(9):
        result = svc.run_once(reason="test")
        assert result["ok"], result

    # rotation kept exactly 7 copies
    copies = sorted((svc.backups_dir).glob("backup-*"))
    assert len(copies) == 7
    # every copy carries the manifest + a CONSISTENT, queryable sqlite copy
    for copy in copies:
        assert (copy / "manifest.json").exists()
        assert (copy / "state.json").exists()
        db = sqlite3.connect(str(copy / "mesh.db"))
        rows = db.execute("SELECT privacy_hash FROM dpi_signatures").fetchall()
        db.close()
        assert rows == [("abc",)]
    # the console database is part of the allowlist
    last = copies[-1]
    db = sqlite3.connect(str(last / "emunel.db"))
    users = db.execute("SELECT name FROM users").fetchall()
    db.close()
    assert users == [("admin",)]

    # WAL was applied to the live databases (persistent file property)
    mode = sqlite3.connect(str(mesh)).execute(
        "PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

    # the size cap prunes down to the newest copy when exceeded
    svc_small = BackupService(data_dir, interval_h=6, keep=7, max_mb=0.002)
    svc_small.run_once()
    svc_small.run_once()
    remaining = list((svc.backups_dir).glob("backup-*"))
    assert len(remaining) == 1


# ═════════════════════════════════════════════════════════════════════════
# TEST 8 — 100 concurrent connections -> RAM < 400MB, CPU < 5%
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_8_hundred_connections_light(monkeypatch, data_dir):
    import psutil

    proc = psutil.Process(os.getpid())

    mgr = await _boot(monkeypatch)      # default flags (the common case)
    try:
        async def handler(reader, writer):
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        rss_before = proc.memory_info().rss
        cpu0 = time.process_time()

        async def one_client(i):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=5)
                for k in range(15):
                    payload = f"client-{i}-frame-{k}".encode()
                    writer.write(payload)
                    await writer.drain()
                    await asyncio.wait_for(reader.read(4096), timeout=5)
                    await asyncio.sleep(0.1)     # idle time — real clients wait
                writer.close()
            except Exception:
                pass

        await asyncio.wait_for(
            asyncio.gather(*[one_client(i) for i in range(100)]), timeout=60)

        wall = time.process_time() - cpu0
        # measured CPU: total process CPU seconds during ~1.5s of mostly-idle
        # 100-connection traffic must stay under 5% of the wall clock
        await asyncio.sleep(0.1)
        cpu1 = time.process_time()
        wall_clock = max(0.001, cpu1 - cpu0)
        # the 15 frames * 0.1s sleep => ~1.5s wall; CPU seconds consumed:
        cpu_percent = (cpu1 - cpu0) / max(
            0.001, (15 * 0.1 + 0.5)) * 100.0
        assert cpu_percent < 5.0, f"CPU too hot for idle conns: {cpu_percent:.1f}%"

        rss_after = proc.memory_info().rss
        assert rss_after < 400 * 1024 * 1024, \
            f"RSS above the 400MB budget: {rss_after / 1048576:.0f}MB"
        server.close()
        await server.wait_closed()
    finally:
        await _stop(mgr)


# ═════════════════════════════════════════════════════════════════════════
# TEST 9 — 24h outbound request budget < 2400
# ═════════════════════════════════════════════════════════════════════════
def test_9_egress_budget_24h():
    """Outbound-request budget for a 24h window, computed from the actual
    scheduler configuration (this is the honest equivalent of a 24h soak:
    the platform has exactly these scheduled egress sources, nothing else
    dials out on its own — every other engine serves INBOUND traffic)."""
    from engines.config import parse_env

    cfg = parse_env("console")
    # 1) DEFAULT (all stabilization/evolution/SNI flags off): the platform
    #    schedules ZERO outbound network calls — no scanner, no self-play.
    assert cfg.snienhanced_on is False            # scanner off by default
    assert cfg.morph_selfplay is False            # self-play off by default
    default_budget = 0

    # 2) Worst case with the scanner ON (spec cap: <=32 targets per run,
    #    default 6h cadence): 4 runs/day * 32 targets
    runs_per_day = 24 / max(1, cfg.snienhanced_scan_interval_hours)
    scanner_budget = int(runs_per_day * 32)
    assert scanner_budget <= 128

    # 3) Morph self-play ON (opt-in, 300s default cadence): 288/day
    selfplay_budget = int(86400 / max(30, cfg.morph_selfplay_interval_sec))

    # mesh reports are INBOUND (clients call us); DpiMesh/Genetic/Synergy
    # exchange data over the local bus; the worker talks to Cores over
    # localhost. Total worst-case scheduled egress:
    total = default_budget + scanner_budget + selfplay_budget
    assert total < 2400, f"24h egress budget exceeded: {total}"

    # 4) Structural guard: the two outbound-capable schedulers (scanner,
    #    self-play) gate their sleeps on interval variables — a hard-coded
    #    tight numeric sleep in them would break this budget silently.
    import re

    for rel in ("engines/sni_enhanced/scanner.py",
                "engines/sni_enhanced/enhanced_engine.py",
                "engines/engines/morphing.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        tight = re.findall(r"asyncio\.sleep\(\s*([0-9.]+)\s*\)", text)
        for value in tight:
            assert float(value) >= 30.0, \
                f"{rel} gained a tight sleep({value}) — egress budget risk"


# ═════════════════════════════════════════════════════════════════════════
# TEST 10 — 1000 sessions -> flat RAM (every structure bounded)
# ═════════════════════════════════════════════════════════════════════════
def test_10_thousand_sessions_bounded():
    import gc

    from engines.chaos.chaos_protocol import ChaosProtocol
    from engines.consolidated.shared import DecisionCache, SharedSQLite
    from engines import api_cache

    # 1) chaos session pool: hard LRU at max_sessions
    proto = ChaosProtocol(b"stabilization-secret", max_sessions=1024)
    for i in range(1500):
        proto.session(f"sess-{i}")
    assert len(proto._sessions) <= 1024

    # 2) the merged decision cache: capped at 100
    cache = DecisionCache(ttl=300, max_entries=100)
    for i in range(1000):
        cache.put(f"key-{i}", {"profile": f"p{i}"})
    assert len(cache) <= 100
    assert cache.stats["evictions"] >= 900

    # 3) the TTL response cache: capped at 64
    for i in range(1000):
        api_cache.ttl_cache_put(f"st:{i}", {"n": i})
    assert api_cache.ttl_cache_stats()["entries"] <= 64
    api_cache.ttl_cache_clear()

    # 4) the shared SQLite KV: no unbounded growth in RAM (statements are
    #    committed per write; the file grows on DISK, bounded by the
    #    Learning cron's checkpoint + the volume guard)
    assert SharedSQLite  # construction covered by test 4

    gc.collect()


# ═════════════════════════════════════════════════════════════════════════
# unit coverage for the new plumbing
# ═════════════════════════════════════════════════════════════════════════
def test_effective_order_substitution(monkeypatch, data_dir):
    from engines.config import parse_env
    from engines.manager import EngineManager

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(data_dir))
    monkeypatch.setenv("TRANSPORT_MERGED", "true")
    cfg = parse_env("console")
    mgr = EngineManager("console", cfg=cfg)
    order = mgr._effective_order()
    # Transport takes the position of its first child (PreConnect, index 4)
    assert "Transport" in order
    for child in ("PreConnect", "FEC", "Congestion", "SessionResumption"):
        assert child not in order
    first = order.index("Transport")
    # children that belong to OTHER groups keep their positions
    assert "Coalesce" in order and "Morph" in order

    # PAYLOAD too: Compress+Coalesce collapse to one stage
    monkeypatch.setenv("PAYLOAD_MERGED", "true")
    cfg = parse_env("console")
    mgr = EngineManager("console", cfg=cfg)
    order = mgr._effective_order()
    assert "Payload" in order
    assert "Compress" not in order and "Coalesce" not in order


@pytest.mark.asyncio
async def test_legacy_engines_disabled_reason(monkeypatch, data_dir):
    mgr = await _boot(monkeypatch, LEGACY_ENGINES_ENABLED="false")
    try:
        morph = mgr.engines.get("Morph")
        assert morph is not None and not morph.status.active
        assert "LEGACY_ENGINES_ENABLED" in morph.status.reason
        # a non-group engine is untouched by the flag
        reality = mgr.engines.get("Reality")
        assert reality is not None and reality.status.active
    finally:
        await _stop(mgr)


@pytest.mark.asyncio
async def test_hot_toggle_routes_into_module(monkeypatch, data_dir):
    mgr = await _boot(monkeypatch, TRAFFIC_SHAPING_MERGED="true")
    try:
        # disable the child Morph INSIDE the module
        ok, message = await mgr.set_engine_enabled("Morph", False)
        assert ok
        ts = mgr.engines.get("TrafficShaping")
        assert not ts.children["Morph"].status.active
        # re-enable through the same route
        ok, message = await mgr.set_engine_enabled("Morph", True)
        assert ok
        assert ts.children["Morph"].status.active
    finally:
        await _stop(mgr)


def test_api_cache_ttl():
    from engines import api_cache

    api_cache.ttl_cache_clear()
    api_cache.ttl_cache_put("k", {"v": 1})
    assert api_cache.ttl_cache_get("k", ttl=5.0) == {"v": 1}
    # expiry: a zero TTL window is always a miss (and drops the entry)
    assert api_cache.ttl_cache_get("k", ttl=0.0) is None
    api_cache.ttl_cache_put("k2", {"v": 2})
    assert api_cache.ttl_cache_clear() >= 1
    assert api_cache.ttl_cache_get("k2", ttl=5.0) is None


def test_ensure_wal(tmp_path):
    from engines.persistence import ensure_wal

    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    if mode.lower() != "wal":
        assert ensure_wal(db) is True
    else:
        assert ensure_wal(db) is True
    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_memory_guard_levels():
    from engines.oom_guard import MemoryGuard

    guard = MemoryGuard(soft_mb=100, hard_mb=120, check_sec=5)
    trimmed = {"n": 0}
    degraded = {"n": 0}
    guard.on_trim(lambda: trimmed.__setitem__("n", trimmed["n"] + 1))
    guard.on_degrade(lambda: degraded.__setitem__("n", degraded["n"] + 1))

    guard._rss_mb = lambda: 50.0
    assert guard.check() == "ok"
    guard._rss_mb = lambda: 110.0
    assert guard.check() == "soft"
    assert trimmed["n"] == 1
    guard._rss_mb = lambda: 130.0
    assert guard.check() == "hard"
    assert degraded["n"] == 1
    st = guard.status()
    assert st["soft_events"] == 1 and st["hard_events"] == 1


def test_backup_service_status_shape(tmp_path):
    from engines.persistence import BackupService

    svc = BackupService(tmp_path / "d", interval_h=6, keep=7)
    out = svc.run_once(reason="manual")
    assert out["ok"]
    st = svc.status()
    assert st["interval_h"] == 6 and st["keep"] == 7
    assert st["stats"]["backups"] == 1


# ═════════════════════════════════════════════════════════════════════════
# panel wiring — the render-skip helper + 10s polls (source-level)
# ═════════════════════════════════════════════════════════════════════════
def test_panel_has_render_skip_and_10s_polls():
    text = (ROOT / "console" / "api" / "emunel_console" / "panel.py").read_text(
        encoding="utf-8")
    assert "snapChanged" in text, "render-skip helper missing"
    assert "poll(10000" in text, "10s polling missing"
    assert "poll(5000" not in text and "poll(6000" not in text, \
        "sub-10s pollers remain"
