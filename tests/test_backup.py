"""Backup regressions. Only temporary databases and mocked PostgreSQL; no stack startup."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "api"))

from fastapi import FastAPI, HTTPException
import httpx
from emunel_console.db import _SqliteDatabase, SQLITE_SCHEMA
from emunel_console.services import backup
from emunel_console.routers import backup as router
from emunel_console.auth import sessions

SECRET = "synthetic-backup-secret-not-real-0123456789"
NOW = "2026-01-01T00:00:00+00:00"
USER, INSTANCE, WORKER, DOMAIN, LINK = [f"{i:032x}" for i in range(1, 6)]


def fixture():
    return {
        "format": backup.FORMAT, "format_version": 1, "created_at": NOW,
        "source_backend": "sqlite", "endpoint_secret": SECRET,
        "tables": {
            "users": [{"id": USER, "github_id": 1234, "login": "original", "name": "Original",
                       "email": None, "avatar_url": None, "is_admin": True, "is_disabled": False,
                       "password_hash": "synthetic-hash", "created_at": NOW, "last_login_at": None}],
            "instances": [{"id": INSTANCE, "user_id": USER, "name": "Sample", "slug": "sample",
                           "region": "local", "status": "running", "provider": "railway",
                           "provider_ref": "foreign-service", "core_api_token": "synthetic-core-token",
                           "created_at": NOW, "updated_at": NOW, "last_active_at": NOW,
                           "public_host": "sample.invalid"}],
            "instance_configs": [{"instance_id": INSTANCE, "protocol": "vless-ws", "cpu_limit": 0.5,
                                   "memory_mb": 256, "max_processes": 128, "link_quota_bytes": 1000,
                                   "core_version": "latest", "protocols": 'vless-ws,trojan-ws',
                                   "updated_at": NOW}],
            "workers": [{"id": WORKER, "node_id": "node", "region": "local", "driver": "process",
                         "enabled": True, "capacity": 20, "created_at": NOW}],
            "domains": [{"id": DOMAIN, "instance_id": INSTANCE, "domain": "opaque-endpoint-token",
                         "kind": "path", "is_custom": False, "is_active": True,
                         "provider_ref": "foreign-domain", "tls": True, "created_at": NOW}],
            "instance_links": [{"id": LINK, "instance_id": INSTANCE, "link_uuid": "synthetic-link-uuid",
                                "label": "Example", "created_at": NOW}],
        },
    }


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = _SqliteDatabase(str(Path(self.temp.name) / "target.db"))
        await self.db.connect()
        await self.db.conn_executescript(SQLITE_SCHEMA)
        self.doc = fixture()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def restore(self, doc=None, dry_run=False):
        return await backup.restore_backup(self.db, doc or self.doc, SECRET, dry_run=dry_run)

    async def test_roundtrip_credentials_and_safe_states(self):
        result = await self.restore()
        self.assertTrue(result["ok"])
        self.assertFalse(result["runtime_state_restored"])
        raw = await backup.export_backup(self.db, SECRET)
        exported = backup.parse(raw)
        backup.validate(exported)
        tables = exported["tables"]
        self.assertEqual(exported["endpoint_secret"], SECRET)
        self.assertEqual(tables["users"][0]["password_hash"], "synthetic-hash")
        self.assertEqual(tables["instances"][0]["core_api_token"], "synthetic-core-token")
        self.assertEqual(tables["instance_links"], self.doc["tables"]["instance_links"])
        self.assertEqual(tables["instance_configs"], self.doc["tables"]["instance_configs"])
        self.assertEqual(tables["instances"][0]["status"], "stopped")
        self.assertIsNone(tables["instances"][0]["provider_ref"])
        self.assertIsNone(tables["instances"][0]["public_host"])
        self.assertFalse(tables["workers"][0]["enabled"])
        self.assertFalse(tables["domains"][0]["is_active"])
        for table in ("sessions", "oauth_states", "deployments", "metrics"):
            self.assertNotIn(table, tables)
            self.assertEqual(await self.db.fetchval(f"SELECT COUNT(*) FROM {table}"), 0)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM activity_events"), 1)
        # Exported JSON itself can be restored to a second, independent DB.
        second = _SqliteDatabase(str(Path(self.temp.name) / "second.db"))
        await second.connect()
        try:
            await second.conn_executescript(SQLITE_SCHEMA)
            await backup.restore_backup(second, exported, SECRET, dry_run=False)
            self.assertEqual(await second.fetchval("SELECT COUNT(*) FROM instances"), 1)
        finally:
            await second.close()

    async def test_dry_run_no_writes_and_repeat_conflicts(self):
        self.assertTrue((await self.restore(dry_run=True))["can_restore"])
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM users"), 0)
        await self.restore()
        result = await self.restore(dry_run=True)
        self.assertFalse(result["can_restore"])
        self.assertNotIn("opaque-endpoint-token", json.dumps(result["conflicts"]))
        with self.assertRaises(HTTPException) as caught:
            await self.restore()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM activity_events"), 1)

    async def test_can_import_alongside_unrelated_admin_and_session(self):
        await self.db.execute("INSERT INTO users (id,login,is_admin,created_at) VALUES ($1,'bootstrap',1,$2)", "f" * 32, NOW)
        await self.db.execute("INSERT INTO sessions (id,user_id,created_at,expires_at) VALUES ('synthetic-session',$1,$2,$2)", "f" * 32, NOW)
        await self.restore()
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM users"), 2)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM sessions"), 1)

    async def test_natural_login_collision(self):
        await self.db.execute("INSERT INTO users (id,login,created_at) VALUES ($1,'ORIGINAL',$2)", "f" * 32, NOW)
        with self.assertRaises(HTTPException) as caught:
            await self.restore()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM instances"), 0)

    async def test_endpoint_key_mismatch(self):
        for secret in ("different", ""):
            with self.assertRaises(HTTPException) as caught:
                await backup.restore_backup(self.db, self.doc, secret, dry_run=False)
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM users"), 0)

    async def test_atomic_rollback_including_audit_failure(self):
        original = backup._SQLiteTransaction.execute
        async def fail(tx, sql, *args):
            if sql.startswith("INSERT INTO activity_events"):
                raise RuntimeError("synthetic failure")
            return await original(tx, sql, *args)
        with patch.object(backup._SQLiteTransaction, "execute", fail):
            with self.assertRaises(RuntimeError):
                await self.restore()
        for table in backup.COLUMNS:
            self.assertEqual(await self.db.fetchval(f"SELECT COUNT(*) FROM {table}"), 0)
        await self.restore()  # lock/connection reusable after rollback

    async def test_concurrent_restore_one_winner(self):
        import asyncio
        results = await asyncio.gather(self.restore(), self.restore(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results), 1)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM instances"), 1)

    def test_strict_validation(self):
        changes = [
            lambda d: d.update(format_version=True),
            lambda d: d.update(format_version=99),
            lambda d: d["tables"].update(sessions=[]),
            lambda d: d["tables"]["users"][0].update(is_admin=1),
            lambda d: d["tables"]["users"][0].update(id="../path"),
            lambda d: d["tables"]["users"][0].update(created_at="2026-01-01T00:00:00"),
            lambda d: d["tables"]["instances"][0].update(user_id="f" * 32),
            lambda d: d["tables"]["instance_configs"].clear(),
            lambda d: d["tables"]["instance_configs"][0].update(cpu_limit=float("nan")),
            lambda d: d["tables"]["instance_configs"][0].update(protocols='{}'),
            lambda d: d["tables"]["instance_configs"][0].update(memory_mb=-1),
            lambda d: d["tables"]["users"].append(dict(d["tables"]["users"][0])),
            lambda d: d["tables"]["users"][0].update(login={}),
        ]
        for change in changes:
            doc = copy.deepcopy(self.doc)
            change(doc)
            with self.subTest(change=change), self.assertRaises(HTTPException) as caught:
                backup.validate(doc)
            self.assertEqual(caught.exception.status_code, 400)

    def test_parser_bounds_and_duplicates(self):
        for raw in (b'{"a":1,"a":2}', b'{"n":NaN}', b'\xff', b'not JSON', b'[' * 2000):
            with self.assertRaises(HTTPException):
                backup.parse(raw)
        with patch.object(backup, "MAX_BYTES", 10):
            with self.assertRaises(HTTPException) as caught:
                backup.parse(b" " * 11)
            self.assertEqual(caught.exception.status_code, 413)
        with patch.object(backup, "MAX_ROWS", 1):
            with self.assertRaises(HTTPException) as caught:
                backup.validate(self.doc)
            self.assertEqual(caught.exception.status_code, 413)

    async def test_export_bounds(self):
        await self.restore()
        with patch.object(backup, "MAX_ROWS", 1):
            with self.assertRaises(HTTPException) as caught:
                await backup.export_backup(self.db, SECRET)
            self.assertEqual(caught.exception.status_code, 413)

    async def test_router_auth_csrf_confirmation_and_roundtrip(self):
        app = FastAPI()
        app.include_router(router.router)
        user = {"id": "f" * 32, "is_admin": True}
        auth = AsyncMock(return_value=None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("emunel_console.routers.admin.get_pool", return_value=self.db), patch.object(router, "get_pool", return_value=self.db), patch.object(sessions, "get_session_user", auth), patch.object(router.settings, "secret_key", SECRET):
                self.assertEqual((await client.post("/api/admin/backup/export")).status_code, 401)
                auth.return_value = {"id": USER, "is_admin": False}
                self.assertEqual((await client.get("/api/admin/backup")).status_code, 403)
                auth.return_value = user
                client.cookies.set("emunel_session", "synthetic-session")
                self.assertEqual((await client.post("/api/admin/backup/export")).status_code, 403)
                client.headers["X-EMUNEL-CSRF"] = "synthetic-session"
                cap = await client.get("/api/admin/backup")
                self.assertFalse(cap.json()["runtime_state_included"])
                check = await client.post("/api/admin/backup/validate", json=self.doc)
                self.assertTrue(check.json()["can_restore"])
                self.assertEqual(check.headers["cache-control"], "no-store")
                self.assertEqual((await client.post("/api/admin/backup/restore", json=self.doc)).status_code, 400)
                response = await client.post("/api/admin/backup/restore", json=self.doc, headers={"X-EMUNEL-Backup-Confirm": "import"})
                self.assertEqual(response.status_code, 200, response.text)
                response = await client.post("/api/admin/backup/export")
                self.assertIn("attachment", response.headers["content-disposition"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json()["endpoint_secret"], SECRET)
                response = await client.post("/api/admin/backup/restore", json=self.doc, headers={"X-EMUNEL-Backup-Confirm": "import"})
                self.assertEqual(response.status_code, 409)
                self.assertEqual((await client.post("/api/admin/backup/validate", content=b"{}", headers={"Content-Type": "text/plain"})).status_code, 415)
                self.assertEqual((await client.post("/api/admin/backup/validate", content=b"{}", headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})).status_code, 415)
                with patch.object(backup, "MAX_BYTES", 10):
                    response = await client.post("/api/admin/backup/validate", content=b" " * 11, headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 413)
                    async def chunks():
                        yield b" " * 6
                        yield b" " * 6
                    response = await client.post("/api/admin/backup/validate", content=chunks(), headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 413)


class PostgresAdapterTests(unittest.IsolatedAsyncioTestCase):
    def fake(self):
        db = MagicMock(mode="postgres")
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        db._pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        db._pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        return db, conn

    async def test_transaction_scoping_lock_and_types(self):
        db, conn = self.fake()
        result = await backup.restore_backup(db, fixture(), SECRET, dry_run=False)
        self.assertTrue(result["ok"])
        conn.transaction.assert_called_once_with(isolation="read_committed", readonly=False)
        calls = conn.execute.call_args_list
        self.assertTrue(calls[0].args[0].startswith("LOCK TABLE users"))
        user_insert = next(c for c in calls if c.args[0].startswith("INSERT INTO users"))
        self.assertIsInstance(user_insert.args[1], UUID)
        self.assertIsNotNone(user_insert.args[-2].tzinfo)
        self.assertIsInstance(user_insert.args[7], bool)
        db._pool.execute.assert_not_called()
        db._pool.fetch.assert_not_called()

    async def test_snapshot_is_repeatable_read(self):
        db, conn = self.fake()
        result = backup.parse(await backup.export_backup(db, SECRET))
        self.assertEqual(result["source_backend"], "postgres")
        conn.transaction.assert_called_once_with(isolation="repeatable_read", readonly=True)
        conn.execute.assert_not_called()

    async def test_error_reaches_transaction_exit(self):
        db, conn = self.fake()
        conn.execute.side_effect = [None, RuntimeError("insertion failed")]
        with self.assertRaises(RuntimeError):
            await backup.restore_backup(db, fixture(), SECRET, dry_run=False)
        self.assertEqual(conn.transaction.return_value.__aexit__.call_args.args[0], RuntimeError)
        db._pool.acquire.return_value.__aexit__.assert_awaited_once()
        # No partial writes: the exception aborted before the audit insert.
        inserted = [c.args[0].split()[2] for c in conn.execute.call_args_list
                    if c.args[0].startswith("INSERT INTO")]
        self.assertEqual(inserted, ["users"])


if __name__ == "__main__":
    unittest.main()
