"""Portable Console configuration snapshots; no runtime or filesystem mutations.

The DB facades autocommit individual calls. This service deliberately uses one
transaction-scoped underlying connection for the snapshot/check/insert sequence.
Only this adapter accesses the facade's private connection/lock/pool attributes.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
from fastapi import HTTPException

FORMAT = "emunel-console-configuration"
VERSION = 1
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10000
# Explicit allowlists prevent accidental export of sessions or new secret columns.
COLUMNS = {
    "users": "id github_id login name email avatar_url is_admin is_disabled password_hash created_at last_login_at".split(),
    "instances": "id user_id name slug region status provider provider_ref core_api_token created_at updated_at last_active_at public_host".split(),
    "instance_configs": "instance_id protocol cpu_limit memory_mb max_processes link_quota_bytes core_version protocols updated_at".split(),
    "workers": "id node_id region driver enabled capacity created_at".split(),
    "domains": "id instance_id domain kind is_custom is_active provider_ref tls created_at".split(),
    "instance_links": "id instance_id link_uuid label created_at".split(),
}
BOOLS = {"is_admin", "is_disabled", "enabled", "is_custom", "is_active", "tls"}
IDS = {"id", "user_id", "instance_id"}
DATES = {"created_at", "updated_at", "last_login_at", "last_active_at"}
INTS = {"github_id", "memory_mb", "max_processes", "link_quota_bytes", "capacity"}
NULLABLE = {"github_id", "name", "email", "avatar_url", "password_hash", "last_login_at",
            "provider", "provider_ref", "last_active_at", "public_host", "protocols"}
WARNINGS = [
    "Sensitive plaintext: contains password hashes, Core API tokens, link identifiers and EMUNEL_SECRET_KEY. Store encrypted offline; never publish.",
    "Console configuration only, NOT a complete runtime backup: Core link passwords (including Shadowsocks), policy/usage state and worker registry/files are excluded.",
    "Restore is insert-only. All ID, login, GitHub ID, slug, node and domain collisions reject the entire import. Existing data and sessions remain unchanged.",
    "Imported instances are stopped, provider references/public hosts cleared, domains inactive and workers disabled. No deployment or runtime API call is made.",
    "Reconcile worker registry/Core state and DNS/provider ownership before manually deploying. Existing Core APIs cannot export all secrets or restore full state. Recreating links can change credentials.",
    "The target EMUNEL_SECRET_KEY must match the backup to preserve endpoint tokens; configure it securely out of band before import. Changing it invalidates existing target endpoint tokens.",
]
EXCLUDED = ["sessions", "oauth_states", "deployments", "deployment_logs", "metrics", "activity_events",
            "worker_registry", "core_state", "worker_tokens", "database_credentials", "oauth_credentials", "provider_credentials"]


def capabilities():
    return {"format": FORMAT, "format_version": VERSION, "scope": "console_configuration",
            "max_bytes": MAX_BYTES, "max_rows": MAX_ROWS, "tables": list(COLUMNS),
            "restore_mode": "insert_only", "runtime_state_included": False,
            "excluded": EXCLUDED, "warnings": WARNINGS}


def invalid(detail="Invalid backup document"):
    raise HTTPException(400, detail=detail)


def encode(doc):
    raw = json.dumps(doc, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, detail="Backup exceeds 16 MiB limit")
    return raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid("Duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def parse(raw):
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, detail="Backup exceeds 16 MiB limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: invalid("Non-finite JSON numbers are not allowed"))
    except (ValueError, UnicodeError, RecursionError):
        invalid("Backup must be UTF-8 JSON (not compressed or multipart)")


def _date(value):
    if not isinstance(value, str) or len(value) > 40:
        invalid("Invalid timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            invalid("Timestamps must include a timezone")
        return result
    except ValueError:
        invalid("Invalid timestamp")


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        invalid("IDs must be UUIDs or 32 hexadecimal characters")
    return UUID(value).hex


def _unique_keys(table, row):
    id_col = "instance_id" if table == "instance_configs" else "id"
    keys = [("id", _identity(str(row[id_col])))]
    if table == "users":
        keys.append(("login", row["login"].casefold()))
        if row["github_id"] is not None:
            keys.append(("github_id", row["github_id"]))
    elif table == "instances":
        keys.append(("owner_slug", _identity(str(row["user_id"])), row["slug"].casefold()))
    elif table == "workers":
        keys.append(("node_id", row["node_id"].casefold()))
    elif table == "domains":
        keys.append(("domain", row["domain"].casefold()))
    elif table == "instance_links":
        keys.append(("link_uuid", _identity(str(row["instance_id"])), row["link_uuid"].casefold()))
    return keys


def validate(doc):
    if not isinstance(doc, dict) or set(doc) != {"format", "format_version", "created_at", "source_backend", "endpoint_secret", "tables"}:
        invalid("Unexpected or missing backup envelope fields")
    if doc["format"] != FORMAT or type(doc["format_version"]) is not int or doc["format_version"] != VERSION:
        invalid("Unsupported backup format/version")
    if doc["source_backend"] not in ("sqlite", "postgres"):
        invalid("Invalid source backend")
    _date(doc["created_at"])
    if not isinstance(doc["endpoint_secret"], str) or len(doc["endpoint_secret"]) > 4096:
        invalid("Invalid endpoint secret")
    tables = doc["tables"]
    if not isinstance(tables, dict) or set(tables) != set(COLUMNS):
        invalid("Unexpected or missing backup tables")
    count = 0
    for table, columns in COLUMNS.items():
        rows = tables[table]
        if not isinstance(rows, list):
            invalid("Table rows must be arrays")
        count += len(rows)
        if count > MAX_ROWS:
            raise HTTPException(413, detail="Backup exceeds 10000 configuration rows")
        keys = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(columns):
                invalid(f"Unexpected or missing columns in {table}")
            for col, value in row.items():
                if value is None:
                    if col not in NULLABLE or (col == "name" and table != "users"):
                        invalid(f"Null not allowed for {table}.{col}")
                elif col in IDS:
                    _identity(value)
                elif col in DATES:
                    _date(value)
                elif col in BOOLS:
                    if type(value) is not bool:
                        invalid(f"Expected boolean for {table}.{col}")
                elif col in INTS:
                    upper = 2**63 - 1 if col in ("github_id", "link_quota_bytes") else 2**31 - 1
                    if type(value) is not int or not 0 <= value <= upper:
                        invalid(f"Invalid integer for {table}.{col}")
                elif col == "cpu_limit":
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1024:
                        invalid("Invalid CPU limit")
                elif not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                    invalid(f"Invalid text for {table}.{col}")
            for col in ("login", "name", "slug", "node_id", "region", "driver", "domain", "core_api_token", "core_version", "link_uuid", "protocol"):
                if col in row and not (table == "users" and col == "name") and not row[col].strip():
                    invalid(f"Empty required text in {table}.{col}")
            if table == "instance_configs" and row["protocols"] is not None:
                try:
                    protocols = (json.loads(row["protocols"]) if row["protocols"].lstrip().startswith(("[", "{"))
                                 else row["protocols"].split(","))
                except (ValueError, RecursionError):
                    invalid("protocols must contain a comma-separated list or JSON array")
                if not isinstance(protocols, list) or len(protocols) > 64 or any(not isinstance(p, str) or not p or len(p) > 80 for p in protocols):
                    invalid("Invalid protocols array")
            for key in _unique_keys(table, row):
                if key in keys:
                    invalid(f"Duplicate {table} {key[0]} in backup")
                keys.add(key)
    # Require a self-contained graph, even in merge mode; never attach uploaded
    # children to existing deployments by guessing a matching parent ID.
    users = {r["id"] for r in tables["users"]}
    instances = {r["id"] for r in tables["instances"]}
    for row in tables["instances"]:
        if row["user_id"] not in users:
            invalid("Instance references a missing backup user")
    for table in ("instance_configs", "domains", "instance_links"):
        if any(r["instance_id"] not in instances for r in tables[table]):
            invalid(f"{table} references a missing backup instance")
    if {r["instance_id"] for r in tables["instance_configs"]} != instances:
        invalid("Every instance must have exactly one configuration")
    return {table: len(rows) for table, rows in tables.items()}


class _SQLiteTransaction:
    def __init__(self, db):
        self.db = db

    async def fetch(self, sql, *args):
        query, params = self.db._translate(sql, args)
        async with self.db._conn.execute(query, params) as cursor:
            cols = [c[0] for c in cursor.description]
            return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def execute(self, sql, *args):
        query, params = self.db._translate(sql, args)
        async with self.db._conn.execute(query, params):
            pass


@asynccontextmanager
async def transaction(db, *, write=False):
    if db.mode == "sqlite":
        async with db._lock:
            tx = _SQLiteTransaction(db)
            await tx.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield tx
                await db._conn.commit()
            except BaseException:
                await db._conn.rollback()
                raise
    elif db.mode == "postgres":
        async with db._pool.acquire() as conn:
            async with conn.transaction(isolation="read_committed" if write else "repeatable_read", readonly=not write):
                if write:
                    # Serializes conflict checks against all ordinary DML too,
                    # not merely other restore requests in this process.
                    await conn.execute("LOCK TABLE " + ", ".join(COLUMNS) + " IN SHARE ROW EXCLUSIVE MODE")
                yield conn
    else:
        raise HTTPException(503, detail="Unsupported database backend")


def _json_row(row):
    result = {}
    for col, value in dict(row).items():
        if col in BOOLS:
            value = bool(value)
        elif isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        result[col] = value
    return result


async def export_backup(db, endpoint_secret):
    tables = {}
    size = 0
    count = 0
    async with transaction(db) as tx:
        for table, columns in COLUMNS.items():
            rows = await tx.fetch(f"SELECT {', '.join(columns)} FROM {table} LIMIT {MAX_ROWS + 1}")
            count += len(rows)
            if count > MAX_ROWS:
                raise HTTPException(413, detail="Backup exceeds 10000 configuration rows")
            tables[table] = [_json_row(row) for row in rows]
            size += len(encode(tables[table]))
            if size > MAX_BYTES:
                raise HTTPException(413, detail="Backup exceeds 16 MiB limit")
    doc = {"format": FORMAT, "format_version": VERSION,
           "created_at": datetime.now(timezone.utc).isoformat(), "source_backend": db.mode,
           "endpoint_secret": endpoint_secret, "tables": tables}
    validate(doc)
    return encode(doc)


async def find_conflicts(tx, doc, endpoint_secret):
    """Diff the document against live rows without echoing secret values."""
    conflicts = []
    if not endpoint_secret or not secrets.compare_digest(doc["endpoint_secret"].encode(), endpoint_secret.encode()):
        conflicts.append({"table": "settings", "key": "endpoint_secret"})
    for table, columns in COLUMNS.items():
        if not doc["tables"][table]:
            continue
        # Never echo conflicting values: domain tokens and link UUIDs are secrets.
        rows = await tx.fetch(f"SELECT {', '.join(columns)} FROM {table} LIMIT {MAX_ROWS + 1}")
        if len(rows) > MAX_ROWS:
            conflicts.append({"table": table, "key": "target_too_large"})
            continue
        existing = {key for row in rows for key in _unique_keys(table, row)}
        reasons = {key[0] for row in doc["tables"][table] for key in _unique_keys(table, row) if key in existing}
        conflicts.extend({"table": table, "key": reason} for reason in sorted(reasons))
    return conflicts


def _insert_row(table, source, mode):
    row = dict(source)
    if table == "instances":
        row.update(status="stopped", provider=None, provider_ref=None, public_host=None, last_active_at=None)
    elif table == "domains":
        row.update(is_active=False, provider_ref=None)
    elif table == "workers":
        row["enabled"] = False
    if mode == "postgres":
        for col, value in row.items():
            if value is not None and col in IDS:
                row[col] = UUID(value)
            elif value is not None and col in DATES:
                row[col] = _date(value)
    return row


async def restore_backup(db, doc, endpoint_secret, *, dry_run, actor_id=None):
    counts = validate(doc)
    try:
        async with transaction(db, write=not dry_run) as tx:
            conflicts = await find_conflicts(tx, doc, endpoint_secret)
            result = {"ok": not conflicts, "can_restore": not conflicts, "dry_run": dry_run,
                      "counts": counts, "conflicts": conflicts, "warnings": WARNINGS,
                      "runtime_state_restored": False}
            if conflicts:
                if dry_run:
                    return result
                raise HTTPException(409, detail={"error": "Import conflicts; nothing written", "conflicts": conflicts})
            if dry_run:
                return result
            for table, columns in COLUMNS.items():
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('$' + str(i + 1) for i in range(len(columns)))})"
                for source in doc["tables"][table]:
                    row = _insert_row(table, source, db.mode)
                    await tx.execute(sql, *[row[col] for col in columns])
            await tx.execute("INSERT INTO activity_events (user_id, kind, level, message, created_at) VALUES ($1, 'admin', 'warn', $2, $3)",
                             UUID(str(actor_id)) if actor_id and db.mode == "postgres" else actor_id,
                             f"Configuration backup import: {sum(counts.values())} rows; runtime state excluded",
                             datetime.now(timezone.utc))
            result["restored"] = counts
            return result
    except (sqlite3.IntegrityError, asyncpg.IntegrityConstraintViolationError):
        raise HTTPException(409, detail="Database constraint conflict; nothing written") from None
