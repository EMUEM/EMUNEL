"""Per-config traffic policy — the AHB capability set, EMUNEL-style.

Covers:
* parsers (quota KB/MB/GB, expiry days/absolute, speed Mbps, IP limit)
* Link state merging (DB policy + Core live counters)
* core-side enforcement primitives (quota/expiry/active — inherited;
  speed + IP are the new ones)
* subscription aggregation (sum of caps, earliest expiry)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))

from emunel_core.state import (  # noqa: E402
    ConnectionTracker, Link, LinkStore, SpeedLimiter, StateStore, RuntimeStats,
)
from emunel_console.services import links as links_svc  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_quota_units(self):
        for body, want in (
            ({"limit": 100, "unit": "MB"}, 100 * 1024 ** 2),
            ({"limit": 2, "unit": "GB"}, 2 * 1024 ** 3),
            ({"limit": 512, "unit": "KB"}, 512 * 1024),
            ({"limit": 1, "unit": "TB"}, 1024 ** 4),
            ({"limit_bytes": 5 * 1024 ** 2}, 5 * 1024 ** 2),
        ):
            self.assertEqual(links_svc.parse_quota(body), want, body)
        # empty / zero / null all mean unlimited (None)
        for body in ({}, {"limit": ""}, {"limit": 0}, {"limit": None},
                     {"limit_bytes": ""}, {"limit_bytes": 0}, {"limit_bytes": None}):
            self.assertIsNone(links_svc.parse_quota(body), body)

    def test_quota_validation(self):
        for body in ({"limit": -5}, {"limit": "abc"}, {"limit_bytes": "x"},
                     {"limit": 1, "unit": "PB"}, {"limit": 10 ** 6, "unit": "TB"}):
            with self.assertRaises(links_svc.ValidationError, msg=body):
                links_svc.parse_quota(body)

    def test_expiry_days_and_absolute(self):
        days, expires = links_svc.parse_expiry({"expiry_days": 30})
        self.assertEqual(days, 30)
        self.assertAlmostEqual(expires.timestamp(),
                               (datetime.now(timezone.utc) + timedelta(days=30)).timestamp(),
                               delta=5)
        when = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        days2, expires2 = links_svc.parse_expiry({"expires_at": when})
        self.assertAlmostEqual(days2, 7, delta=0.01)
        self.assertAlmostEqual(expires2.timestamp(), datetime.fromisoformat(when).timestamp(), delta=2)
        # empty means never
        self.assertEqual(links_svc.parse_expiry({}), (None, None))
        self.assertEqual(links_svc.parse_expiry({"expiry_days": ""}), (None, None))
        self.assertEqual(links_svc.parse_expiry({"expiry_days": 0}), (None, None))

    def test_speed_mbps_ahb_formula(self):
        # AHB: MBIT -> value * 1024 * 1024 / 8
        self.assertEqual(links_svc.parse_speed({"speed_mbps": 8}), 1024 * 1024)
        self.assertEqual(links_svc.parse_speed({}), 0)
        self.assertEqual(links_svc.parse_speed({"speed_mbps": ""}), 0)
        with self.assertRaises(links_svc.ValidationError):
            links_svc.parse_speed({"speed_mbps": -1})

    def test_ip_limit(self):
        self.assertEqual(links_svc.parse_ip_limit({"ip_limit": 3}), 3)
        self.assertEqual(links_svc.parse_ip_limit({}), 0)
        self.assertEqual(links_svc.parse_ip_limit({"ip_limit": ""}), 0)
        with self.assertRaises(links_svc.ValidationError):
            links_svc.parse_ip_limit({"ip_limit": -2})

    def test_wizard_policy_none_when_empty(self):
        self.assertIsNone(links_svc.parse_wizard_policy({}))
        self.assertIsNone(links_svc.parse_wizard_policy({"limit": "", "expiry_days": ""}))
        policy = links_svc.parse_wizard_policy({"limit": 1, "unit": "GB", "expiry_days": 30})
        self.assertEqual(policy["limit_bytes"], 1024 ** 3)
        self.assertIsNotNone(policy["expires_at"])


class CoreLinkPolicyTests(unittest.IsolatedAsyncioTestCase):
    """The enforcement primitives the relays rely on (Link/LinkStore)."""

    def test_new_fields_default_off(self):
        link = Link(uuid="a" * 32, label="L", protocol="vless-ws")
        self.assertEqual(link.speed_limit_bytes, 0)
        self.assertEqual(link.ip_limit, 0)
        data = link.to_dict()
        self.assertEqual(data["speed_limit_bytes"], 0)
        self.assertEqual(data["ip_limit"], 0)

    async def test_ip_limit_allows_existing_ip_blocks_new(self):
        store = LinkStore()
        conn = ConnectionTracker()
        await store.add(Link(uuid="a" * 32, label="L", protocol="vless-ws", ip_limit=2))
        link = store.get("a" * 32)
        conn.register("c1", uuid="a" * 32, ip="1.1.1.1", transport="vless-ws")
        conn.register("c2", uuid="a" * 32, ip="2.2.2.2", transport="vless-ws")
        # a third *distinct* IP is over the cap…
        self.assertFalse(conn.ip_allowed(link, "3.3.3.3"))
        # …while IPs already connected keep working
        self.assertTrue(conn.ip_allowed(link, "1.1.1.1"))
        # removing a connection frees a slot
        conn.remove("c2")
        self.assertTrue(conn.ip_allowed(link, "3.3.3.3"))
        # limit=0 means unlimited
        await store.add(Link(uuid="b" * 32, label="M", protocol="vless-ws"))
        self.assertTrue(conn.ip_allowed(store.get("b" * 32), "9.9.9.9"))

    async def test_unique_ips_for(self):
        conn = ConnectionTracker()
        conn.register("c1", uuid="u1", ip="1.1.1.1", transport="t")
        conn.register("c2", uuid="u1", ip="1.1.1.1", transport="t")
        conn.register("c3", uuid="u1", ip="2.2.2.2", transport="t")
        conn.register("c4", uuid="u2", ip="3.3.3.3", transport="t")
        self.assertEqual(conn.unique_ips_for("u1"), {"1.1.1.1", "2.2.2.2"})
        self.assertEqual(conn.unique_ips_for("u2"), {"3.3.3.3"})

    async def test_speed_limiter_paces_and_skips_when_off(self):
        limiter = SpeedLimiter()
        # rate=0 → no-op (fast)
        await asyncio.wait_for(limiter.consume("u", 0, 10 * 1024 ** 2), timeout=0.1)
        # generous bucket: small chunk passes instantly
        await asyncio.wait_for(limiter.consume("u", 1024 ** 2, 4096), timeout=0.5)
        # changing the rate rebuilds the bucket (AHB _get_bucket behavior)
        await asyncio.wait_for(limiter.consume("u", 2048, 100), timeout=0.5)
        limiter.reset("u")

    async def test_token_bucket_sleeps_when_empty(self):
        limiter = SpeedLimiter()
        # 1 KB/s floor with a 16 KB burst: 2x10 KB chunks — the second must
        # wait for the bucket to refill (~4s at 1 KB/s).
        import time

        await asyncio.wait_for(limiter.consume("u", 1024, 10 * 1024), timeout=0.5)
        start = time.monotonic()
        await asyncio.wait_for(limiter.consume("u", 1024, 10 * 1024), timeout=8)
        took = time.monotonic() - start
        self.assertGreaterEqual(took, 2.0, "token bucket did not pace past the burst")

    async def test_token_bucket_handles_chunks_larger_than_burst(self):
        """Regression: a single chunk bigger than the burst capacity must be
        consumed slice-by-side, not awaited as one lump that can never
        accumulate (the AHB original hangs forever on such chunks)."""
        import time

        limiter = SpeedLimiter()
        start = time.monotonic()
        # 1 MB chunk at 1 KB/s with a 16 KB burst: ~1010 s if correct-by-wait
        # would be absurd — the slice-wise path must finish fast for the
        # *immediately available* part and pace the rest. Here: 1 MB at
        # 1 MB/s finishes in well under a second of pacing budget; use a rate
        # that makes the timing decisive: 1 MB chunk at 1 MB/s = ~1 s.
        await asyncio.wait_for(limiter.consume("u", 1024 * 1024, 1024 * 1024), timeout=6)
        took = time.monotonic() - start
        self.assertLess(took, 6.0, "large chunk wedged the bucket")
        self.assertGreaterEqual(took, 0.0)

    async def test_state_persists_new_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            store = LinkStore()
            stats = RuntimeStats()
            await store.add(Link(uuid="c" * 32, label="L", protocol="vless-ws",
                                 limit_bytes=1024, speed_limit_bytes=2048, ip_limit=3))
            saver = StateStore(path)
            await saver.save(store, stats)
            # fresh store loads the same policy
            store2 = LinkStore()
            stats2 = RuntimeStats()
            await StateStore(path).load(store2, stats2)
            loaded = store2.get("c" * 32)
            self.assertEqual(loaded.speed_limit_bytes, 2048)
            self.assertEqual(loaded.ip_limit, 3)
            self.assertEqual(loaded.limit_bytes, 1024)

    async def test_old_state_file_still_loads(self):
        """Links saved before this feature (no speed/ip keys) must load with
        the new fields defaulting to off."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.json"
            path.write_text(json.dumps({
                "version": 1,
                "total_bytes": 5,
                "hourly": {},
                "links": [{
                    "uuid": "d" * 32, "label": "old", "protocol": "vless-ws",
                    "active": True, "limit_bytes": 10, "used_bytes": 2,
                    "created_at": "2026-01-01T00:00:00+00:00", "expires_at": None,
                    "note": "", "alpn": "http/1.1", "fingerprint": "chrome",
                    "ss_cipher": None, "ss_password": None,
                }],
            }), encoding="utf-8")
            store = LinkStore()
            await StateStore(str(path)).load(store, RuntimeStats())
            link = store.get("d" * 32)
            self.assertIsNotNone(link)
            self.assertEqual(link.speed_limit_bytes, 0)
            self.assertEqual(link.ip_limit, 0)


class AggregateQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from emunel_console.db import _SqliteDatabase, SQLITE_SCHEMA, _sqlite_patch_schema

        self.temp = tempfile.TemporaryDirectory()
        self.db = _SqliteDatabase(str(Path(self.temp.name) / "l.db"))
        await self.db.connect()
        await self.db.conn_executescript(SQLITE_SCHEMA)
        await _sqlite_patch_schema(self.db)
        self.pool = self.db

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _mk_instance(self) -> str:
        from datetime import datetime as dt

        await self.pool.execute(
            "INSERT INTO instances (id, user_id, name, slug, region, status, provider, "
            "core_api_token, created_at, updated_at) VALUES ($1,'u','i','i','local','running',"
            "'local','t',$2,$2)",
            "inst1", dt.now(timezone.utc),
        )
        return "inst1"

    async def test_aggregate_sums_caps_and_earliest_expiry(self):
        iid = await self._mk_instance()
        soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        await self.pool.execute(
            "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
            "limit_bytes, expires_at, speed_limit_bytes, ip_limit, active, used_cache, created_at) "
            "VALUES ('1',$1,'a','A','vless-ws',$2,$3,0,0,1,0,$5)",
            iid, 100 * 1024 ** 2, soon, None, datetime.now(timezone.utc).isoformat(),
        )
        await self.pool.execute(
            "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
            "limit_bytes, expires_at, speed_limit_bytes, ip_limit, active, used_cache, created_at) "
            "VALUES ('2',$1,'b','B','trojan-ws',$2,$3,0,0,1,0,$5)",
            iid, 50 * 1024 ** 2, later, None, datetime.now(timezone.utc).isoformat(),
        )
        agg = await links_svc.aggregate_quota(self.pool, iid, used_bytes=25 * 1024 ** 2)
        self.assertEqual(agg["limit_bytes"], 150 * 1024 ** 2)
        self.assertEqual(agg["used_bytes"], 25 * 1024 ** 2)
        self.assertEqual(agg["remaining_bytes"], 125 * 1024 ** 2)
        self.assertAlmostEqual(
            datetime.fromisoformat(agg["expires_at"]).timestamp(),
            datetime.fromisoformat(soon).timestamp(), delta=2,
        )
        self.assertFalse(agg["exceeded"])

    async def test_aggregate_none_when_all_unlimited(self):
        iid = await self._mk_instance()
        self.assertIsNone(await links_svc.aggregate_quota(self.pool, iid, used_bytes=10))
        # inactive links don't count toward the aggregate
        await self.pool.execute(
            "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
            "limit_bytes, active, used_cache, created_at) "
            "VALUES ('1',$1,'a','A','vless-ws',$2,0,0,$3)",
            iid, 1024 ** 3, datetime.now(timezone.utc).isoformat(),
        )
        self.assertIsNone(await links_svc.aggregate_quota(self.pool, iid, used_bytes=10))


class LinkOutTests(unittest.TestCase):
    def _row(self, **over):
        base = {
            "link_uuid": "u1", "label": "A", "protocol": "vless-ws", "limit_bytes": 100,
            "expires_at": None, "speed_limit_bytes": 0, "ip_limit": 0, "active": 1,
            "used_cache": 40, "used_at": None,
        }
        base.update(over)
        return base

    def test_status_matrix(self):
        self.assertEqual(links_svc._link_out(self._row())["status"], "active")
        self.assertEqual(links_svc._link_out(self._row(used_cache=100))["status"], "limited")
        self.assertEqual(links_svc._link_out(self._row(active=0))["status"], "disabled")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertEqual(links_svc._link_out(self._row(expires_at=past))["status"], "expired")

    def test_unlimited_semantics(self):
        out = links_svc._link_out(self._row(limit_bytes=None))
        self.assertTrue(out["unlimited"])
        self.assertIsNone(out["limit_bytes"])
        self.assertIsNone(out["remaining_bytes"])
        self.assertEqual(out["status"], "active")


if __name__ == "__main__":
    unittest.main()
