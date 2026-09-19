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


class ParseTimeLimitTests(unittest.TestCase):
    def test_empty_means_unlimited(self):
        self.assertEqual(volume.parse_time_limit({}), (None, None))
        self.assertEqual(volume.parse_time_limit({"time_limit_days": None}), (None, None))
        self.assertEqual(volume.parse_time_limit({"time_limit_days": ""}), (None, None))
        self.assertEqual(volume.parse_time_limit({"time_limit_days": "  "}), (None, None))
        self.assertEqual(volume.parse_time_limit({"time_limit_days": 0}), (None, None))

    def test_days_produce_future_expiry(self):
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        days, expires_at = volume.parse_time_limit({"time_limit_days": 30})
        after = datetime.now(timezone.utc)
        self.assertEqual(days, 30.0)
        target = before.timestamp() + 30 * 86400
        self.assertTrue(before.timestamp() <= expires_at.timestamp() <= after.timestamp() + 30 * 86400)
        self.assertAlmostEqual(expires_at.timestamp(), target, delta=5)
        # fractional days are accepted (e.g. half a day)
        days, _ = volume.parse_time_limit({"time_limit_days": 0.5})
        self.assertEqual(days, 0.5)

    def test_bounds(self):
        with self.assertRaises(ValueError):
            volume.parse_time_limit({"time_limit_days": -1})
        with self.assertRaises(ValueError):
            volume.parse_time_limit({"time_limit_days": "abc"})
        with self.assertRaises(ValueError):   # below one minute
            volume.parse_time_limit({"time_limit_days": 1 / 86400 / 2})
        with self.assertRaises(ValueError):   # above ten years
            volume.parse_time_limit({"time_limit_days": 4000})


class TimeLimitStateTests(VolumeBase):
    async def test_default_state_has_no_time_limit(self):
        with self.core_total(0):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertIsNone(state["time_limit_days"])
        self.assertIsNone(state["expires_at"])
        self.assertFalse(state["expired"])
        self.assertIsNone(state["seconds_remaining"])

    async def test_set_and_clear_time_limit(self):
        from datetime import datetime, timedelta, timezone
        days, expires_at = volume.parse_time_limit({"time_limit_days": 30})
        await volume.set_time_limit(self.db, INSTANCE, days, expires_at)
        state = await volume.get_state(self.db, INSTANCE)
        self.assertEqual(state["time_limit_days"], 30.0)
        self.assertFalse(state["expired"])
        self.assertGreater(state["seconds_remaining"], 29 * 86400)
        self.assertLessEqual(state["seconds_remaining"], 30 * 86400)
        # volume is untouched — the two limits are independent
        self.assertTrue(state["unlimited"])
        # clear → back to default/unlimited
        await volume.set_time_limit(self.db, INSTANCE, None, None)
        state = await volume.get_state(self.db, INSTANCE)
        self.assertIsNone(state["expires_at"])
        self.assertFalse(state["expired"])

    async def test_volume_and_time_coexist(self):
        await volume.set_limit(self.db, INSTANCE, 50 * GB)
        days, expires_at = volume.parse_time_limit({"time_limit_days": 7})
        await volume.set_time_limit(self.db, INSTANCE, days, expires_at)
        with self.core_total(5 * GB):
            state = await volume.get_state(self.db, INSTANCE)
        self.assertEqual(state["limit_bytes"], 50 * GB)
        self.assertEqual(state["used_bytes"], 5 * GB)
        self.assertEqual(state["time_limit_days"], 7.0)
        self.assertFalse(state["expired"])

    async def test_expired_detected(self):
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await volume.set_time_limit(self.db, INSTANCE, 1, past)
        state = await volume.get_state(self.db, INSTANCE)
        self.assertTrue(state["expired"])
        self.assertEqual(state["seconds_remaining"], 0)
        reached, info = await volume.time_reached(self.db, INSTANCE)
        self.assertTrue(reached)
        self.assertTrue(info["expires_at"].startswith(past.strftime("%Y-%m-%dT%H")))

    async def test_not_yet_expired_does_not_gate(self):
        from datetime import datetime, timezone
        days, expires_at = volume.parse_time_limit({"time_limit_days": 30})
        await volume.set_time_limit(self.db, INSTANCE, days, expires_at)
        reached, info = await volume.time_reached(self.db, INSTANCE)
        self.assertFalse(reached)
        self.assertIsNone(info)


class TimeEnforcementTests(VolumeBase):
    async def test_expired_running_instance_is_stopped(self):
        from datetime import datetime, timedelta, timezone
        await self.db.execute(
            "UPDATE instances SET status='running' WHERE id=$1", INSTANCE)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        await volume.set_time_limit(self.db, INSTANCE, 30, past)
        stopped = AsyncMock()
        with patch.object(volume.deploy_svc, "stop_instance", stopped):
            await volume.enforce_all(self.db)
        stopped.assert_called_once()
        rows = await self.db.fetch(
            "SELECT message FROM activity_events WHERE instance_id = $1", INSTANCE)
        self.assertTrue(any("time limit reached" in r["message"] for r in rows))

    async def test_unexpired_instance_keeps_running(self):
        days, expires_at = volume.parse_time_limit({"time_limit_days": 30})
        await volume.set_time_limit(self.db, INSTANCE, days, expires_at)
        await self.db.execute(
            "UPDATE instances SET status='running' WHERE id=$1", INSTANCE)
        stopped = AsyncMock()
        with patch.object(volume.deploy_svc, "stop_instance", stopped):
            await volume.enforce_all(self.db)
        stopped.assert_not_called()

    async def test_time_pass_does_not_break_volume_pass(self):
        # both limits set: volume over, time fine → stopped by the volume rule
        await self.db.execute(
            "UPDATE instances SET status='running' WHERE id=$1", INSTANCE)
        await volume.set_limit(self.db, INSTANCE, 5 * GB)
        days, expires_at = volume.parse_time_limit({"time_limit_days": 30})
        await volume.set_time_limit(self.db, INSTANCE, days, expires_at)
        stopped = AsyncMock()
        with patch.object(volume.deploy_svc, "stop_instance", stopped), \
             self.core_total(6 * GB):
            await volume.enforce_all(self.db)
        stopped.assert_called_once()
        rows = await self.db.fetch(
            "SELECT message FROM activity_events WHERE instance_id = $1", INSTANCE)
        self.assertTrue(any("volume limit reached" in r["message"] for r in rows))


class UserinfoHeaderTests(unittest.TestCase):
    def test_no_state_is_the_legacy_default(self):
        self.assertEqual(volume.userinfo_header(None),
                         "upload=0; download=0; total=0; expire=0")

    def test_unlimited_state_reports_usage_only(self):
        self.assertEqual(
            volume.userinfo_header({"used_bytes": 1234, "limit_bytes": None,
                                    "expires_at": None}),
            "upload=0; download=1234; total=0; expire=0")

    def test_limits_map_to_total_and_expire(self):
        from datetime import datetime, timezone
        expires_at = datetime.now(timezone.utc)
        header = volume.userinfo_header(
            {"used_bytes": 2 * GB, "limit_bytes": 10 * GB,
             "expires_at": expires_at.isoformat()})
        want_expire = int(expires_at.timestamp())
        self.assertEqual(
            header, f"upload=0; download={2 * GB}; total={10 * GB}; expire={want_expire}")

    def test_corrupt_expiry_falls_back_to_unlimited(self):
        header = volume.userinfo_header(
            {"used_bytes": 0, "limit_bytes": None, "expires_at": "not-a-date"})
        self.assertIn("expire=0", header)


class SubscriptionPageTests(unittest.TestCase):
    """The subscription page must show the instance's REAL limits."""

    @staticmethod
    def _render(quota=None):
        from emunel_console.services.subscription import render_subscription
        return render_subscription(
            "EMUNEL \u00b7 test", [], "example.com", "/i/tok/sub",
            qr_path="/i/tok/api/qr", quota=quota)

    def test_no_quota_keeps_legacy_look(self):
        page = self._render()
        self.assertIn("Not reported / ∞", page)
        self.assertIn("Unlimited", page)
        self.assertIn("s-active", page)
        self.assertIn(">Active<", page)

    def test_volume_and_time_rendered(self):
        from datetime import datetime, timedelta, timezone
        expires_at = datetime.now(timezone.utc) + timedelta(days=29)
        quota = {
            "limit_bytes": 50 * GB, "used_bytes": 10 * GB,
            "remaining_bytes": 40 * GB, "percent": 20.0, "exceeded": False,
            "time_limit_days": 30, "expires_at": expires_at.isoformat(),
            "seconds_remaining": 29 * 86400, "expired": False,
        }
        page = self._render(quota)
        self.assertIn("10.00 GB", page)          # usage
        self.assertIn("of 50.00 GB", page)       # the real cap
        self.assertIn("40.00 GB", page)          # remaining
        self.assertIn("until", page)
        self.assertIn("29d", page)               # time remaining
        self.assertNotIn("Not reported", page)
        self.assertNotIn(">Unlimited<", page)    # hmm — time card no longer unlimited
        self.assertIn("s-active", page)

    def test_expired_and_exceeded_status(self):
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(days=1)
        quota = {
            "limit_bytes": None, "used_bytes": 0, "exceeded": False,
            "time_limit_days": 30, "expires_at": past.isoformat(),
            "seconds_remaining": 0, "expired": True,
        }
        page = self._render(quota)
        self.assertIn("s-expired", page)
        self.assertIn(">Expired<", page)
        # exceeded (but still within the time window) → Limited
        future = datetime.now(timezone.utc) + timedelta(days=5)
        quota = {
            "limit_bytes": 1024, "used_bytes": 4096, "exceeded": True,
            "time_limit_days": 30, "expires_at": future.isoformat(),
            "seconds_remaining": 5 * 86400, "expired": False,
        }
        page = self._render(quota)
        self.assertIn("s-limited", page)
        self.assertIn(">Limited<", page)


if __name__ == "__main__":
    unittest.main()
