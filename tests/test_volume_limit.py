"""Volume-limit regressions — console-side accounting only.

Covers: empty value = Default/unlimited, limit set/clear round-trip, usage
baseline math (reset), the deploy gate, and enforcement (stopping through the
normal lifecycle service). All against an isolated SQLite database with the
worker/Core calls stubbed; the relay Core itself is never touched.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "api"))

from emunel_console.db import _SqliteDatabase, SQLITE_SCHEMA
from emunel_console.services import volume

USER = "a" * 32
INSTANCE = "b" * 32
GB = 1024 ** 3


class VolumeBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = _SqliteDatabase(str(Path(self.temp.name) / "v.db"))
        await self.db.connect()
        await self.db.conn_executescript(SQLITE_SCHEMA)
        await self.db.execute(
            "INSERT INTO users (id, login, name, is_admin, created_at) "
            "VALUES ($1, 'admin', 'Administrator', 1, '2026-01-01T00:00:00+00:00')",
            USER,
        )
        await self.db.execute(
            "INSERT INTO instances (id, user_id, name, slug, status, core_api_token,"
            " created_at, updated_at) VALUES ($1, $2, 'VolTest', 'voltest', 'stopped',"
            " 'tok', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            INSTANCE, USER,
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def core_total(self, n):
        """Stub the live Core stats fetch to report n lifetime bytes."""
        return patch.object(volume, "_live_total", AsyncMock(return_value=n))


class ParseLimitTests(unittest.TestCase):
    def test_empty_means_unlimited(self):
        self.assertIsNone(volume.parse_limit({}))
        self.assertIsNone(volume.parse_limit({"limit_gb": None}))
        self.assertIsNone(volume.parse_limit({"limit_gb": ""}))
        self.assertIsNone(volume.parse_limit({"limit_gb": 0}))
        self.assertIsNone(volume.parse_limit({"limit_bytes": 0}))

    def test_gb_and_bytes_accepted(self):
        self.assertEqual(volume.parse_limit({"limit_gb": 50}), 50 * GB)
        self.assertEqual(volume.parse_limit({"limit_gb": 1.5}), int(1.5 * GB))
        self.assertEqual(volume.parse_limit({"limit_bytes": 5 * GB}), 5 * GB)

    def test_bounds(self):
        with self.assertRaises(ValueError):
            volume.parse_limit({"limit_gb": 0.0005})   # below 1 MB
        with self.assertRaises(ValueError):
            volume.parse_limit({"limit_bytes": 1024 ** 6})  # above 1 PB
        with self.assertRaises(ValueError):
            volume.parse_limit({"limit_gb": "abc"})


class VolumeStateTests(VolumeBase):
    async def test_default_state_is_unlimited_with_cached_fallback(self):
        with self.core_total(3 * GB):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertTrue(state["unlimited"])
        self.assertIsNone(state["limit_bytes"])
        self.assertEqual(state["used_bytes"], 3 * GB)
        self.assertTrue(state["live"])
        self.assertFalse(state["exceeded"])

    async def test_core_down_uses_cache_not_an_error(self):
        with patch.object(volume, "_live_total", AsyncMock(return_value=None)):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertFalse(state["live"])
        self.assertEqual(state["used_bytes"], 0)

    async def test_set_and_clear_limit(self):
        await volume.set_limit(self.db, INSTANCE, 50 * GB)
        with self.core_total(10 * GB):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertFalse(state["unlimited"])
        self.assertEqual(state["limit_bytes"], 50 * GB)
        self.assertEqual(state["used_bytes"], 10 * GB)
        self.assertAlmostEqual(state["percent"], 20.0)
        self.assertEqual(state["remaining_bytes"], 40 * GB)
        # clear → back to default/unlimited
        await volume.set_limit(self.db, INSTANCE, None)
        with self.core_total(10 * GB):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertTrue(state["unlimited"])
        self.assertIsNone(state["percent"])

    async def test_reset_moves_counter_below_baseline(self):
        await volume.set_limit(self.db, INSTANCE, 50 * GB)
        with self.core_total(30 * GB):
            await volume.get_state(self.db, INSTANCE)      # prime the cache
        await volume.reset_usage(self.db, INSTANCE)
        with self.core_total(45 * GB):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertEqual(state["used_bytes"], 15 * GB)      # 45 lifetime - 30 baseline
        self.assertAlmostEqual(state["percent"], 30.0)

    async def test_reset_with_core_down_uses_cached_usage(self):
        await volume.set_limit(self.db, INSTANCE, 50 * GB)
        with self.core_total(30 * GB):
            await volume.get_state(self.db, INSTANCE)
        with patch.object(volume, "_live_total", AsyncMock(return_value=None)):
            await volume.reset_usage(self.db, INSTANCE)    # baseline += cached 30 GB
        with self.core_total(31 * GB):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertEqual(state["used_bytes"], 1 * GB)


class GateAndEnforcementTests(VolumeBase):
    async def test_deploy_gate_only_trips_when_reached(self):
        await volume.set_limit(self.db, INSTANCE, 10 * GB)
        reached, info = await volume.limit_reached(self.db, INSTANCE)
        self.assertFalse(reached)                          # no usage yet
        with self.core_total(10 * GB):
            await volume.get_state(self.db, INSTANCE)      # cache = 10 GB
        reached, info = await volume.limit_reached(self.db, INSTANCE)
        self.assertTrue(reached)
        self.assertEqual(info["limit_bytes"], 10 * GB)

    async def test_no_limit_never_gates(self):
        with self.core_total(999 * GB):
            await volume.get_state(self.db, INSTANCE)
        reached, info = await volume.limit_reached(self.db, INSTANCE)
        self.assertFalse(reached)
        self.assertIsNone(info)

    async def test_enforcement_stops_only_over_limit_running_instances(self):
        stopped = AsyncMock()
        with patch.object(volume.deploy_svc, "stop_instance", stopped):
            # unlimited instance: nothing happens
            await self.db.execute(
                "UPDATE instances SET status='running' WHERE id=$1", INSTANCE)
            await volume.enforce_all(self.db)
            stopped.assert_not_called()

            # capped but under the limit: nothing happens
            await volume.set_limit(self.db, INSTANCE, 50 * GB)
            with self.core_total(10 * GB):
                await volume.enforce_all(self.db)
            stopped.assert_not_called()

            # capped and over: stopped through the normal lifecycle service
            await volume.set_limit(self.db, INSTANCE, 5 * GB)
            with self.core_total(6 * GB):
                await volume.enforce_all(self.db)
            stopped.assert_called_once()
            stopped.reset_mock()

            # capped, over, but already stopped: not touched
            await self.db.execute(
                "UPDATE instances SET status='stopped' WHERE id=$1", INSTANCE)
            await volume.enforce_all(self.db)
            stopped.assert_not_called()

    async def test_enforcement_records_activity(self):
        await self.db.execute(
            "UPDATE instances SET status='running' WHERE id=$1", INSTANCE)
        await volume.set_limit(self.db, INSTANCE, 1 * GB)
        with patch.object(volume.deploy_svc, "stop_instance", AsyncMock()), \
             self.core_total(2 * GB):
            await volume.enforce_all(self.db)
        rows = await self.db.fetch(
            "SELECT message FROM activity_events WHERE instance_id = $1", INSTANCE)
        self.assertTrue(any("volume limit reached" in r["message"] for r in rows))

    async def test_enforcement_never_raises(self):
        # broken stats fetch must not propagate
        with patch.object(volume, "_live_total",
                          AsyncMock(side_effect=RuntimeError("boom"))):
            await volume.enforce_all(self.db)  # no exception
        # broken query must not propagate either
        with patch.object(self.db, "fetch", AsyncMock(side_effect=RuntimeError("db down"))):
            await volume.enforce_all(self.db)


if __name__ == "__main__":
    unittest.main()
