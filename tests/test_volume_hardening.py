"""Volume-limit hardening tests — the AHB-bypass closure.

The AHB panel lost its usage counter on every restart (state only saved on
admin actions), which is how a capped deployment relayed ~1TB. EMUNEL's
per-config quota was already relay-time and persisted; these tests pin the
NEW instance-wide guarantees:

  * the Core enforces the instance lifetime cap AT RELAY TIME (QuotaGate),
    not only from the Console's polling loop;
  * the cap survives Core restarts (state file roundtrip);
  * /core/api/quota is token-guarded and validates input;
  * the Console pushes baseline+limit to the Core on set/reset;
  * a blind enforcement loop (stats unreachable) now ALERTS instead of
    silently allowing traffic, and repairs core-state regression so a
    wiped state file cannot renew the quota.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "api"))

from emunel_console.db import SQLITE_SCHEMA, _SqliteDatabase  # noqa: E402
from emunel_console.services import volume  # noqa: E402
from emunel_core.config import CoreConfig  # noqa: E402
from emunel_core.relay.base import QuotaGate, RelayContext  # noqa: E402
from emunel_core.state import (  # noqa: E402
    ConnectionTracker, Link, LinkStore, RuntimeStats, SpeedLimiter, StateStore)


def _loop():
    return asyncio.new_event_loop()


# ── Core: relay-time instance cap ────────────────────────────────────────────
def test_quota_gate_cuts_at_instance_cap():
    """Relay-time cut, with the SAME bounded-overshoot semantics as the
    per-link quota: accounting is batched (adaptive, <=2MB / 0.25s), so the
    cut lands at most one batch beyond the cap — never an unbounded flow."""
    async def main():
        links = LinkStore()
        await links.add(Link(uuid="u1", label="l", protocol="vless-ws",
                             limit_bytes=0))            # per-link unlimited
        stats = RuntimeStats()
        stats.instance_cap_bytes = 1000                   # instance cap 1000B
        ctx = RelayContext(links=links, connections=ConnectionTracker(),
                           stats=stats, save_hook=None, cfg=None)
        gate = QuotaGate(ctx, "u1")
        assert await gate.add(600) is True                 # batched (returns True)
        assert await gate.flush() is True                  # 600 accounted
        assert await gate.add(500) is True                 # batched again
        assert await gate.flush() is True                  # crossed inside one batch
        assert stats.total_bytes == 1100                   # 600 + 500 = one batch over
        # once the cap is hit, every further frame is denied IMMEDIATELY
        # (pre-batch check — no more data flows on this gate)
        assert await gate.add(1) is False
        assert stats.total_bytes == 1100                   # denied batch not counted
        assert stats.instance_cap_hits >= 1
        assert await gate.flush() is False
        # a NEW connection opened after the cap is cut on its FIRST frame
        gate2 = QuotaGate(ctx, "u1")
        assert await gate2.add(1) is False
        assert stats.total_bytes == 1100

    _loop().run_until_complete(main())


def test_quota_gate_ignores_cap_when_zero():
    async def main():
        links = LinkStore()
        await links.add(Link(uuid="u1", label="l", protocol="vless-ws"))
        stats = RuntimeStats()
        ctx = RelayContext(links=links, connections=ConnectionTracker(),
                           stats=stats, save_hook=None, cfg=None)
        gate = QuotaGate(ctx, "u1")
        assert await gate.add(10_000_000) is True          # unlimited

    _loop().run_until_complete(main())


def test_instance_cap_survives_state_roundtrip(tmp_path):
    async def main():
        path = str(tmp_path / "state.json")
        links = LinkStore()
        await links.add(Link(uuid="u1", label="l", protocol="vless-ws",
                             used_bytes=123))
        stats = RuntimeStats()
        stats.total_bytes = 123
        stats.instance_cap_bytes = 5000
        store = StateStore(path)
        await store.save(links, stats)
        # fresh Core process, same state file
        links2, stats2 = LinkStore(), RuntimeStats()
        await StateStore(path).load(links2, stats2)
        assert stats2.instance_cap_bytes == 5000            # cap persisted
        assert stats2.total_bytes == 123
        assert links2.get("u1").used_bytes == 123
        # old-format state files (no cap key) default to unlimited
        raw = json.loads(Path(path).read_text())
        del raw["instance_cap_bytes"]
        Path(path).write_text(json.dumps(raw))
        stats3 = RuntimeStats()
        await StateStore(path).load(LinkStore(), stats3)
        assert stats3.instance_cap_bytes == 0

    _loop().run_until_complete(main())


# ── Core: /core/api/quota management route ───────────────────────────────────
@pytest.fixture()
def core_app(tmp_path):
    from fastapi.testclient import TestClient

    from emunel_core.app import create_app
    from emunel_core.config import CoreConfig
    client = TestClient(create_app(CoreConfig(
        api_token="test-token", state_path=str(tmp_path / "s.json"),
        log_level="error")))
    yield client


def test_core_quota_route_guarded_and_functional(core_app):
    # token-guarded
    assert core_app.get("/core/api/quota").status_code == 401
    h = {"Authorization": "Bearer test-token"}
    # default unlimited
    r = core_app.get("/core/api/quota", headers=h)
    assert r.status_code == 200 and r.json() == {
        "cap_bytes": 0, "used_bytes": 0, "enforced": False, "cap_hits": 0}
    # set a cap
    r = core_app.put("/core/api/quota", headers=h,
                     json={"cap_bytes": 9000})
    assert r.status_code == 200 and r.json()["cap_bytes"] == 9000
    assert core_app.get("/core/api/quota", headers=h).json()["enforced"] is True
    # nonsense rejected
    assert core_app.put("/core/api/quota", headers=h,
                        json={"cap_bytes": "abc"}).status_code == 400
    assert core_app.put("/core/api/quota", headers=h,
                        json={"cap_bytes": 10 ** 18}).status_code == 400
    # clear
    r = core_app.put("/core/api/quota", headers=h, json={"cap_bytes": 0})
    assert r.status_code == 200 and r.json()["cap_bytes"] == 0
    assert core_app.get("/core/api/quota", headers=h).json()["enforced"] is False


def test_core_stats_reports_the_relay_path_totals(core_app):
    h = {"Authorization": "Bearer test-token"}
    core_app.put("/core/api/quota", headers=h, json={"cap_bytes": 100})
    r = core_app.get("/core/api/stats", headers=h).json()
    assert r["total_bytes"] == 0


# ── Console: cap push + blind-loop alerting ─────────────────────────────────
USER = "a" * 32
INSTANCE = "b" * 32
GB = 1024 ** 3


class ConsoleBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = _SqliteDatabase(str(Path(self.temp.name) / "h.db"))
        await self.db.connect()
        await self.db.conn_executescript(SQLITE_SCHEMA)
        await self.db.execute(
            "INSERT INTO users (id, login, name, is_admin, created_at) "
            "VALUES ($1, 'admin', 'Administrator', 1, '2026-01-01T00:00:00+00:00')",
            USER)
        await self.db.execute(
            "INSERT INTO instances (id, user_id, name, slug, status, core_api_token,"
            " created_at, updated_at) VALUES ($1, $2, 'HardTest', 'hardtest',"
            " 'running', 'tok', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            INSTANCE, USER)
        await self.db.execute(
            "INSERT INTO instance_volume (instance_id, limit_bytes, baseline_bytes,"
            " updated_at) VALUES ($1, $2, 0, '2026-01-01T00:00:00+00:00')",
            INSTANCE, 10 * GB)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def worker_calls(self, log):
        async def fake_worker_call(node_url, method, path, json_body=None, timeout=8.0):
            log.append((method, path, dict(json_body or {})))
            if method == "PUT" and path.endswith("/core/api/quota"):
                return {"ok": True, "cap_bytes": (json_body or {}).get("cap_bytes")}
            if path.endswith("/core/api/stats"):
                return {"total_bytes": self.stats_total}
            return {}
        return patch.object(volume.worker_svc, "worker_call", fake_worker_call)


class CapPushTests(ConsoleBase):
    async def test_set_limit_pushes_the_cap_to_the_core(self):
        pushed = []
        with self.worker_calls(pushed):
            ok = await volume.push_core_cap(self.db, INSTANCE)
        self.assertTrue(ok)
        method, path, body = pushed[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(path.endswith("/core/api/quota"))
        self.assertEqual(body, {"cap_bytes": 10 * GB})   # baseline(0) + limit

    async def test_push_failure_is_reported_not_raised(self):
        async def boom(*a, **k):
            raise volume.worker_svc.WorkerError("down")

        with patch.object(volume.worker_svc, "worker_call", boom):
            ok = await volume.push_core_cap(self.db, INSTANCE)
        self.assertFalse(ok)


class BlindLoopTests(ConsoleBase):
    async def test_unreachable_stats_alerts_instead_of_staying_silent(self):
        volume._stale.pop(INSTANCE, None)
        with patch.object(volume, "_live_total", AsyncMock(return_value=None)), \
                patch.object(volume, "_core_quota", AsyncMock(return_value=None)):
            await volume.enforce_all(self.db)
            await volume.enforce_all(self.db)
        row = await self.db.fetchrow(
            "SELECT level, message FROM activity_events "
            "WHERE instance_id = $1 ORDER BY created_at DESC LIMIT 1", INSTANCE)
        self.assertIsNotNone(row)
        self.assertIn("stale", row["message"])
        self.assertGreaterEqual(volume._stale[INSTANCE]["misses"], 2)

    async def test_recovered_stats_clear_the_stale_flag(self):
        volume._stale.pop(INSTANCE, None)
        self.stats_total = 0
        with patch.object(volume, "_live_total", AsyncMock(return_value=None)), \
                patch.object(volume, "_core_quota", AsyncMock(return_value=None)):
            await volume.enforce_all(self.db)
        self.assertIn(INSTANCE, volume._stale)
        with self.worker_calls([]):
            await volume.enforce_all(self.db)
        self.assertNotIn(INSTANCE, volume._stale)


class RegressionRepairTests(ConsoleBase):
    async def test_wiped_core_state_cannot_renew_the_quota(self):
        # operator had used 8GB of the 10GB cap; the Core state file was lost
        await self.db.execute(
            "UPDATE instance_volume SET used_cache = $2 WHERE instance_id = $1",
            INSTANCE, 8 * GB)
        self.stats_total = int(0.5 * GB)                  # fresh lifetime counter
        pushed = []
        with self.worker_calls(pushed):
            await volume.enforce_all(self.db)
        # the cap was recomputed so only the REMAINING allowance survives:
        # fresh lifetime 0.5GB + (10GB limit - 8GB used) = 2.5GB absolute
        caps = [b["cap_bytes"] for m, p, b in pushed
                if m == "PUT" and p.endswith("/core/api/quota")]
        self.assertEqual(caps[-1], int(0.5 * GB) + (10 * GB - 8 * GB))
        row = await self.db.fetchrow(
            "SELECT baseline_bytes, used_cache FROM instance_volume "
            "WHERE instance_id = $1", INSTANCE)
        self.assertEqual(row["baseline_bytes"], int(0.5 * GB))   # re-anchored
        self.assertEqual(row["used_cache"], 8 * GB)               # floor kept
        events = await self.db.fetch(
            "SELECT message FROM activity_events WHERE instance_id = $1 "
            "AND message LIKE '%repaired%'", INSTANCE)
        self.assertTrue(events)
