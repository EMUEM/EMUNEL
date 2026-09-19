"""Console-side per-config management (services/links.py) — DB + Core contract.

The Core is replaced by an in-memory FakeCore that mimics the real
/core/api/links endpoints, so every test exercises the exact request shapes
the real Core will receive (Create/Edit → Database → Core/Relay direction).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))

from emunel_console.db import _SqliteDatabase, SQLITE_SCHEMA, _sqlite_patch_schema
from emunel_console.services import links as links_svc

USER = "a" * 32
INSTANCE = "b" * 32
MB = 1024 ** 2


class FakeCore:
    """In-memory stand-in for the Core's /core/api/links endpoints."""

    def __init__(self):
        self.links: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, pool, instance_id, method, path, json_body=None, timeout=15.0):
        self.calls.append((method, path, json_body))
        if method == "GET" and path == "links":
            return {"links": [dict(v) for v in self.links.values()]}
        if method == "POST" and path == "links":
            import secrets

            body = dict(json_body or {})
            uuid = body.get("uuid") or secrets.token_hex(16)
            self.links[uuid] = {
                "uuid": uuid, "label": body.get("label", ""),
                "protocol": body.get("protocol", "vless-ws"),
                "limit_bytes": int(body.get("limit_bytes") or 0),
                "used_bytes": int(body.get("used_bytes") or 0),
                "expires_at": body.get("expires_at"),
                "speed_limit_bytes": int(body.get("speed_limit_bytes") or 0),
                "ip_limit": int(body.get("ip_limit") or 0),
                "active": bool(body.get("active", True)),
            }
            return {"ok": True, "uuid": uuid}
        if method == "PATCH" and path.startswith("links/"):
            uuid = path.split("/", 1)[1]
            link = self.links.get(uuid)
            if link is None:
                raise links_svc.worker_svc.WorkerError("worker returned 404: link not found")
            body = dict(json_body or {})
            for key in ("label", "expires_at"):
                if key in body:
                    link[key] = body[key]
            for key in ("limit_bytes", "speed_limit_bytes", "ip_limit"):
                if key in body:
                    link[key] = max(0, int(body[key] or 0))
            if "active" in body:
                link["active"] = bool(body["active"])
            if body.get("reset_usage"):
                link["used_bytes"] = 0
            return {"ok": True}
        if method == "DELETE" and path.startswith("links/"):
            uuid = path.split("/", 1)[1]
            if self.links.pop(uuid, None) is None:
                raise links_svc.worker_svc.WorkerError("worker returned 404: link not found")
            return {"ok": True}
        raise links_svc.worker_svc.WorkerError(f"worker returned 404: {method} {path}")


class LinksServiceBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = _SqliteDatabase(str(Path(self.temp.name) / "l.db"))
        await self.db.connect()
        await self.db.conn_executescript(SQLITE_SCHEMA)
        await _sqlite_patch_schema(self.db)
        await self.db.execute(
            "INSERT INTO users (id, login, name, is_admin, created_at) "
            "VALUES ($1, 'admin', 'Administrator', 1, '2026-01-01T00:00:00+00:00')", USER)
        await self.db.execute(
            "INSERT INTO instances (id, user_id, name, slug, status, core_api_token,"
            " created_at, updated_at) VALUES ($1, $2, 'LTest', 'ltest', 'running',"
            " 'tok', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            INSTANCE, USER)
        self.core = FakeCore()
        self._patch = patch.object(links_svc, "_core", self.core)
        self._patch.start()

    async def asyncTearDown(self):
        self._patch.stop()
        await self.db.close()
        self.temp.cleanup()


class CreateAndUpdateTests(LinksServiceBase):
    async def test_create_pushes_policy_to_core_and_db(self):
        out = await links_svc.create_link(
            self.db, INSTANCE,
            {"protocol": "vless-ws", "label": "Phone", "limit": 100, "unit": "MB",
             "expiry_days": 14, "speed_mbps": 5, "ip_limit": 2},
            user_id=USER)
        method, path, body = self.core.calls[-1]
        self.assertEqual((method, path), ("POST", "links"))
        self.assertEqual(body["limit_bytes"], 100 * MB)
        self.assertEqual(body["speed_limit_bytes"], int(5 * 1024 * 1024 / 8))
        self.assertEqual(body["ip_limit"], 2)
        self.assertIsNotNone(body["expires_at"])
        # console DB row mirrors the policy
        row = await self.db.fetchrow(
            "SELECT * FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
            INSTANCE, out["uuid"])
        self.assertEqual(row["limit_bytes"], 100 * MB)
        self.assertEqual(row["ip_limit"], 2)
        # returned state describes the real thing
        self.assertFalse(out["unlimited"])
        self.assertEqual(out["remaining_bytes"], 100 * MB)
        self.assertEqual(out["status"], "active")

    async def test_create_defaults_are_unlimited(self):
        out = await links_svc.create_link(self.db, INSTANCE, {"protocol": "trojan-ws"})
        self.assertTrue(out["unlimited"])
        self.assertIsNone(out["limit_bytes"])
        self.assertEqual(out["speed_limit_bytes"], 0)
        self.assertEqual(out["ip_limit"], 0)
        self.assertIsNone(out["expires_at"])

    async def test_update_is_key_presence(self):
        created = await links_svc.create_link(
            self.db, INSTANCE, {"limit": 1, "unit": "GB", "expiry_days": 30},
            user_id=USER)
        uuid = created["uuid"]
        # touch ONLY the quota — expiry must survive
        out = await links_svc.update_link(self.db, INSTANCE, uuid, {"limit": 2, "unit": "GB"})
        self.assertEqual(out["limit_bytes"], 2 * 1024 ** 3)
        self.assertIsNotNone(out["expires_at"])
        core_link = self.core.links[uuid]
        self.assertEqual(core_link["limit_bytes"], 2 * 1024 ** 3)
        self.assertIsNotNone(core_link["expires_at"])
        # clearing the quota keeps the expiry too
        out = await links_svc.update_link(self.db, INSTANCE, uuid, {"limit": None})
        self.assertTrue(out["unlimited"])
        self.assertIsNotNone(out["expires_at"])

    async def test_disable_and_enable(self):
        created = await links_svc.create_link(self.db, INSTANCE, {}, user_id=USER)
        uuid = created["uuid"]
        out = await links_svc.update_link(self.db, INSTANCE, uuid, {"active": False})
        self.assertEqual(out["status"], "disabled")
        self.assertFalse(self.core.links[uuid]["active"])
        out = await links_svc.update_link(self.db, INSTANCE, uuid, {"active": True})
        self.assertEqual(out["status"], "active")

    async def test_reset_usage_touches_only_one_link(self):
        first = await links_svc.create_link(self.db, INSTANCE, {}, user_id=USER)
        second = await links_svc.create_link(self.db, INSTANCE, {}, user_id=USER)
        self.core.links[first["uuid"]]["used_bytes"] = 40 * MB
        self.core.links[second["uuid"]]["used_bytes"] = 40 * MB
        out = await links_svc.reset_usage(self.db, INSTANCE, first["uuid"], user_id=USER)
        self.assertEqual(out["used_bytes"], 0)
        self.assertEqual(self.core.links[second["uuid"]]["used_bytes"], 40 * MB)
        # the reset flag reached the Core for the right link only
        resets = [c for c in self.core.calls if c[0] == "PATCH" and (c[2] or {}).get("reset_usage")]
        self.assertEqual(len(resets), 1)
        self.assertTrue(resets[0][1].endswith(first["uuid"]))

    async def test_delete_removes_core_and_db(self):
        created = await links_svc.create_link(self.db, INSTANCE, {}, user_id=USER)
        uuid = created["uuid"]
        await links_svc.delete_link(self.db, INSTANCE, uuid, user_id=USER)
        self.assertNotIn(uuid, self.core.links)
        row = await self.db.fetchrow(
            "SELECT * FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
            INSTANCE, uuid)
        self.assertIsNone(row)

    async def test_unknown_link_raises_lookup(self):
        with self.assertRaises(links_svc.NotFoundError):
            await links_svc.update_link(self.db, INSTANCE, "f" * 32, {"limit": 1})
        with self.assertRaises(links_svc.NotFoundError):
            await links_svc.delete_link(self.db, INSTANCE, "f" * 32)


class ListAndReconcileTests(LinksServiceBase):
    async def test_list_merges_live_usage_and_caches(self):
        created = await links_svc.create_link(
            self.db, INSTANCE, {"limit": 100, "unit": "MB"}, user_id=USER)
        self.core.links[created["uuid"]]["used_bytes"] = 30 * MB
        out = await links_svc.list_links(self.db, INSTANCE)
        self.assertTrue(out["live"])
        link = out["links"][0]
        self.assertEqual(link["used_bytes"], 30 * MB)
        self.assertEqual(link["remaining_bytes"], 70 * MB)
        # usage is cached in the DB for the next offline read
        row = await self.db.fetchrow(
            "SELECT used_cache FROM instance_links WHERE link_uuid = $1", created["uuid"])
        self.assertEqual(row["used_cache"], 30 * MB)

    async def test_list_core_down_uses_cache(self):
        created = await links_svc.create_link(
            self.db, INSTANCE, {"limit": 100, "unit": "MB"}, user_id=USER)
        await self.db.execute(
            "UPDATE instance_links SET used_cache = $2 WHERE link_uuid = $1",
            created["uuid"], 12 * MB)
        with patch.object(links_svc, "_core",
                          AsyncMock(side_effect=links_svc.worker_svc.WorkerError("down"))):
            out = await links_svc.list_links(self.db, INSTANCE)
        self.assertFalse(out["live"])
        self.assertEqual(out["links"][0]["used_bytes"], 12 * MB)

    async def test_list_adopts_core_only_links(self):
        # a link that exists only in the Core (created out-of-band) appears
        self.core.links["e" * 32] = {
            "uuid": "e" * 32, "label": "stray", "protocol": "trojan-ws",
            "limit_bytes": 5 * MB, "used_bytes": 1 * MB, "expires_at": None,
            "speed_limit_bytes": 0, "ip_limit": 0, "active": True}
        out = await links_svc.list_links(self.db, INSTANCE)
        self.assertEqual(len(out["links"]), 1)
        self.assertEqual(out["links"][0]["label"], "stray")
        row = await self.db.fetchrow(
            "SELECT label FROM instance_links WHERE link_uuid = $1", "e" * 32)
        self.assertEqual(row["label"], "stray")

    async def test_reconcile_restores_lost_core_links_with_same_uuid(self):
        created = await links_svc.create_link(
            self.db, INSTANCE, {"limit": 100, "unit": "MB", "expiry_days": 7}, user_id=USER)
        uuid = created["uuid"]
        # the Core loses its state file (fresh service, wiped volume)
        self.core.links.clear()
        self.core.calls.clear()
        await links_svc.reconcile_after_deploy(self.db, INSTANCE, None)
        self.assertIn(uuid, self.core.links)
        restored = self.core.links[uuid]
        self.assertEqual(restored["limit_bytes"], 100 * MB)
        self.assertIsNotNone(restored["expires_at"])

    async def test_reconcile_reapplies_drifted_policy(self):
        created = await links_svc.create_link(
            self.db, INSTANCE, {"limit": 100, "unit": "MB", "ip_limit": 2}, user_id=USER)
        uuid = created["uuid"]
        # someone edited the Core directly and the policy drifted
        self.core.links[uuid]["limit_bytes"] = 0
        self.core.links[uuid]["ip_limit"] = 9
        self.core.calls.clear()
        await links_svc.reconcile_after_deploy(self.db, INSTANCE, None)
        patches = [c for c in self.core.calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(self.core.links[uuid]["limit_bytes"], 100 * MB)
        self.assertEqual(self.core.links[uuid]["ip_limit"], 2)


if __name__ == "__main__":
    unittest.main()
