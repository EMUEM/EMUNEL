"""Per-config (per-link) traffic management — the AHB capability set.

The Console database is the policy source of truth (``instance_links``);
the Core enforces for real at relay time (QuotaGate accounting, accept-time
quota/expiry/disabled/IP gates, token-bucket speed cap). Every mutation is
written to the database first and then pushed to the running Core through
the worker proxy, so the intended path is exactly:

    Create/Edit Config → Database → Core/Relay → Traffic Accounting →
    Quota Enforcement → Subscription → Client

Semantics (inherited from the AHB reference and the existing volume
service — empty value means the Default, i.e. unlimited):

* quota   — ``{limit: <value>, unit: KB|MB|GB}`` or raw ``{limit_bytes}``;
             empty/0/None → unlimited (0 is the wire convention for it)
* expiry  — ``{expiry_days}`` (fractional, from now) or absolute
             ``{expires_at}`` ISO timestamp; empty → never expires
* speed   — ``{speed_mbps}`` → bytes/sec (Mbps * 1024² / 8, AHB formula);
             empty → unlimited
* ip cap  — ``{ip_limit}`` concurrent unique client IPs; empty → unlimited
* usage   — always the Core's real counters (persisted by the Core in its
             state file, cached here when the Core is unreachable)
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import asyncpg

from ..config import settings
from ..logging import get
from . import workers as worker_svc

log = get("runtime", "emunel.console.links")

PROTOCOLS = ("vless-ws", "trojan-ws", "shadowsocks", "xhttp-packet-up", "xhttp-stream-up",
             "trojan-xhttp-packet-up", "trojan-xhttp-stream-up", "vmess-ws")

UNIT_BYTES = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
MIN_LIMIT_BYTES = 1024              # 1 KB — finer than the instance cap on purpose
MAX_LIMIT_BYTES = 1024 ** 5         # 1 PB
MIN_EXPIRY_DAYS = 1 / 1440          # one minute
MAX_EXPIRY_DAYS = 3650              # ten years
MAX_SPEED_BYTES = 10 * 1024 ** 3 / 8 * 8   # 10 Gbit/s in bytes/s
MAX_IP_LIMIT = 999


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotFoundError(LookupError):
    """Config not found in the Console DB."""


class ValidationError(ValueError):
    """Operator-visible input error (surfaces as HTTP 400)."""


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_quota(body: dict) -> int | None:
    """{limit: value, unit: KB|MB|GB} or {limit_bytes}. Empty/0 → None
    (unlimited). Raises ValidationError on nonsense."""
    if "limit_bytes" in body and "limit" not in body:
        raw = body.get("limit_bytes")
        if _empty(raw):
            return None
        try:
            limit = int(float(raw))
        except (TypeError, ValueError):
            raise ValidationError("limit_bytes must be a number")
    else:
        raw = body.get("limit")
        if _empty(raw):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValidationError("limit must be a number")
        if value < 0:
            raise ValidationError("limit cannot be negative — leave it empty for unlimited")
        if value == 0:
            return None
        unit = str(body.get("unit") or "GB").strip().upper()
        if unit not in UNIT_BYTES:
            raise ValidationError("unit must be one of KB, MB, GB, TB")
        limit = int(value * UNIT_BYTES[unit])
    if limit < 0:
        raise ValidationError("limit cannot be negative — leave it empty for unlimited")
    if limit == 0:
        return None
    if limit < MIN_LIMIT_BYTES:
        raise ValidationError("limit is below the 1 KB minimum")
    if limit > MAX_LIMIT_BYTES:
        raise ValidationError("limit is above the 1 PB maximum")
    return limit


def parse_expiry(body: dict) -> tuple[float | None, datetime | None]:
    """{expiry_days} (fractional, from now) or {expires_at} ISO (absolute).
    Empty → (None, None) = never expires."""
    raw_days = body.get("expiry_days")
    raw_at = body.get("expires_at")
    if not _empty(raw_at):
        try:
            dt = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError("expires_at must be an ISO timestamp")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0.0, (dt - _utcnow()).total_seconds() / 86400.0)
        return days, dt
    if _empty(raw_days):
        return None, None
    try:
        days = float(raw_days)
    except (TypeError, ValueError):
        raise ValidationError("expiry_days must be a number")
    if days < 0:
        raise ValidationError("expiry cannot be negative — leave it empty for unlimited")
    if days == 0:
        return None, None
    if days < MIN_EXPIRY_DAYS:
        raise ValidationError("expiry is below the one-minute minimum")
    if days > MAX_EXPIRY_DAYS:
        raise ValidationError("expiry is above the ten-year maximum")
    return days, _utcnow() + timedelta(days=days)


def parse_speed(body: dict) -> int:
    """{speed_mbps} → bytes/sec. Empty/0 → 0 (unlimited)."""
    raw = body.get("speed_mbps")
    if _empty(raw):
        return 0
    try:
        mbps = float(raw)
    except (TypeError, ValueError):
        raise ValidationError("speed_mbps must be a number")
    if mbps < 0:
        raise ValidationError("speed cannot be negative — leave it empty for unlimited")
    if mbps == 0:
        return 0
    speed = int(mbps * 1024 * 1024 / 8)   # AHB parse_speed_to_bytes (MBIT)
    if speed < 1:
        return 0
    if speed > MAX_SPEED_BYTES:
        raise ValidationError("speed is above the 10 Gbit/s maximum")
    return speed


def parse_ip_limit(body: dict) -> int:
    """{ip_limit} concurrent unique IPs. Empty/0 → 0 (unlimited)."""
    raw = body.get("ip_limit")
    if _empty(raw):
        return 0
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ValidationError("ip_limit must be a whole number")
    if value < 0:
        raise ValidationError("ip limit cannot be negative — leave it empty for unlimited")
    if value > MAX_IP_LIMIT:
        raise ValidationError("ip limit is above the 999 maximum")
    return value


def parse_link_policy(body: dict) -> dict:
    """Validate all policy fields at once. Returns the normalized dict:
    {limit_bytes, expires_at (ISO|None), expiry_days, speed_limit_bytes, ip_limit}."""
    limit = parse_quota(body)
    days, expires_at = parse_expiry(body)
    speed = parse_speed(body)
    ip_limit = parse_ip_limit(body)
    return {
        "limit_bytes": limit or 0,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expiry_days": days,
        "speed_limit_bytes": speed,
        "ip_limit": ip_limit,
    }


# ---------------------------------------------------------------------------
# Core access (through the worker proxy — same path every other service uses)
# ---------------------------------------------------------------------------
async def _node_url(pool, instance_id: str) -> str | None:
    row = await pool.fetchrow(
        "SELECT node_id FROM deployments WHERE instance_id = $1 "
        "ORDER BY started_at DESC LIMIT 1",
        instance_id,
    )
    node_id = (row["node_id"] if row else None) or settings.default_worker_node
    return worker_svc.worker_url_for(node_id)


async def _core(pool, instance_id: str, method: str, path: str,
                json_body: dict | None = None, timeout: float = 15.0) -> dict:
    node_url = await _node_url(pool, instance_id)
    return await worker_svc.worker_call(
        node_url, method,
        f"/worker/api/instances/{instance_id}/proxy/core/api/{path.lstrip('/')}",
        json_body=json_body, timeout=timeout,
    )


def _policy_to_core(policy: dict) -> dict:
    """Body for the Core link create/patch endpoints."""
    return {
        "limit_bytes": int(policy.get("limit_bytes") or 0),
        "expires_at": policy.get("expires_at"),
        "speed_limit_bytes": int(policy.get("speed_limit_bytes") or 0),
        "ip_limit": int(policy.get("ip_limit") or 0),
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _link_out(row: asyncpg.Record | dict, live: dict | None = None,
              include_secret: bool = False) -> dict:
    """Merge a DB policy row with the Core's live state (usage, enforcement)."""
    data = dict(row)
    uuid = data.get("link_uuid") or data.get("uuid")
    limit = int(data.get("limit_bytes") or 0) or None
    used = int(data.get("used_cache") or 0)
    speed = int(data.get("speed_limit_bytes") or 0)
    ip_limit = int(data.get("ip_limit") or 0)
    active = bool(data.get("active", True))
    expires_at = data.get("expires_at")
    if live is not None:
        limit = int(live.get("limit_bytes") or 0) or None
        used = int(live.get("used_bytes") or 0)
        active = bool(live.get("active", active))
        expires_at = live.get("expires_at") or expires_at
        speed = int(live.get("speed_limit_bytes") or 0)
        ip_limit = int(live.get("ip_limit") or 0)
    out = {
        "uuid": uuid,
        "label": data.get("label") or "",
        "protocol": data.get("protocol"),
        "active": active,
        "limit_bytes": limit,
        "unlimited": limit is None,
        "used_bytes": used,
        "expires_at": _iso(expires_at),
        "speed_limit_bytes": speed,
        "ip_limit": ip_limit,
    }
    if limit:
        out["remaining_bytes"] = max(0, limit - used)
        out["percent"] = round(min(100.0, used / limit * 100.0), 1)
        out["exceeded"] = used >= limit
    else:
        out["remaining_bytes"] = None
        out["percent"] = None
        out["exceeded"] = False
    expired = False
    seconds_remaining = None
    if out["expires_at"]:
        try:
            dt = datetime.fromisoformat(str(out["expires_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds_remaining = max(0.0, (dt - _utcnow()).total_seconds())
            expired = seconds_remaining <= 0
        except ValueError:
            pass
    out["expired"] = expired
    out["seconds_remaining"] = seconds_remaining
    if not active:
        out["status"] = "disabled"
    elif expired:
        out["status"] = "expired"
    elif out["exceeded"]:
        out["status"] = "limited"
    else:
        out["status"] = "active"
    return out


async def list_links(pool, instance_id: str, *, live: bool = True) -> dict:
    """All configs of an instance: DB policy merged with live Core state.
    Core-only links (created out-of-band) are imported so nothing is hidden."""
    rows = await pool.fetch(
        "SELECT * FROM instance_links WHERE instance_id = $1 ORDER BY created_at",
        instance_id,
    )
    core_links: dict[str, dict] = {}
    reachable = False
    if live:
        try:
            resp = await _core(pool, instance_id, "GET", "links", timeout=8.0)
            core_links = {l["uuid"]: l for l in resp.get("links", [])}
            reachable = True
        except (worker_svc.WorkerError, OSError) as exc:
            log.info("core unreachable for %s link listing: %s", instance_id, exc)
    if reachable:
        await _import_core_only(pool, instance_id, rows, core_links)
        rows = await pool.fetch(
            "SELECT * FROM instance_links WHERE instance_id = $1 ORDER BY created_at",
            instance_id,
        )
    out = []
    for row in rows:
        live_state = core_links.get(row["link_uuid"]) if reachable else None
        item = _link_out(row, live_state)
        if reachable and live_state is not None:
            await pool.execute(
                "UPDATE instance_links SET used_cache = $2, used_at = $3, "
                "limit_bytes = COALESCE($4, limit_bytes), "
                "expires_at = COALESCE($5, expires_at), "
                "speed_limit_bytes = COALESCE($6, speed_limit_bytes), "
                "ip_limit = COALESCE($7, ip_limit), active = $8 "
                "WHERE instance_id = $9 AND link_uuid = $10",
                instance_id, item["used_bytes"], _utcnow(),
                live_state.get("limit_bytes") or None,
                live_state.get("expires_at") or None,
                live_state.get("speed_limit_bytes") or None,
                live_state.get("ip_limit") or None,
                bool(live_state.get("active", True)),
                instance_id, row["link_uuid"],
            )
        out.append(item)
    return {"links": out, "live": reachable}


async def _import_core_only(pool, instance_id: str, rows, core_links: dict) -> None:
    """Adopt links that exist in the Core but not in the Console DB (e.g.
    created before this feature or by direct API use) so the panel can
    manage them too."""
    known = {r["link_uuid"] for r in rows}
    for uuid, link in core_links.items():
        if uuid in known:
            continue
        try:
            await pool.execute(
                "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
                "limit_bytes, expires_at, speed_limit_bytes, ip_limit, active, used_cache, "
                "used_at, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                secrets.token_hex(16), instance_id, uuid,
                str(link.get("label") or "")[:200], link.get("protocol"),
                int(link.get("limit_bytes") or 0) or None,
                link.get("expires_at"), int(link.get("speed_limit_bytes") or 0) or None,
                int(link.get("ip_limit") or 0) or None,
                bool(link.get("active", True)), int(link.get("used_bytes") or 0),
                _utcnow(), _utcnow(),
            )
            log.info("adopted core-only link %s for instance %s", uuid[:8], instance_id[:8])
        except Exception as exc:
            log.warning("could not adopt core link %s: %s", uuid[:8], exc)


async def create_link(pool, instance_id: str, body: dict, *,
                      user_id: str | None = None, inst_name: str = "") -> dict:
    """Create a config: validate → Core (real link) → Console DB row."""
    protocol = str(body.get("protocol") or "vless-ws")
    if protocol not in PROTOCOLS:
        raise ValidationError(f"protocol must be one of {', '.join(PROTOCOLS)}")
    label = str(body.get("label") or "").strip()[:80]
    policy = parse_link_policy(body)

    core_body = {
        "label": label or f"Config {protocol}",
        "protocol": protocol,
        **_policy_to_core(policy),
    }
    resp = await _core(pool, instance_id, "POST", "links", json_body=core_body, timeout=30.0)
    link_uuid = resp["uuid"]
    await pool.execute(
        "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
        "limit_bytes, expires_at, speed_limit_bytes, ip_limit, active, used_cache, "
        "used_at, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,0,NULL,$11)",
        secrets.token_hex(16), instance_id, link_uuid, label or f"Config {protocol}",
        protocol, policy["limit_bytes"] or None, policy["expires_at"],
        policy["speed_limit_bytes"] or None, policy["ip_limit"] or None,
        True, _utcnow(),
    )
    if user_id:
        await _activity(pool, user_id, instance_id,
                        f"Config '{label or link_uuid[:8]}' created"
                        + (f" — quota {_fmt(policy['limit_bytes'])}" if policy["limit_bytes"] else ""))
    row = await pool.fetchrow(
        "SELECT * FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
        instance_id, link_uuid,
    )
    return _link_out(row)


async def update_link(pool, instance_id: str, link_uuid: str, body: dict, *,
                      user_id: str | None = None, inst_name: str = "") -> dict:
    """Edit a config after creation. AHB update_link semantics: only the
    keys present in the body change; absent keys keep their current value."""
    row = await pool.fetchrow(
        "SELECT * FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
        instance_id, link_uuid,
    )
    if row is None:
        raise NotFoundError("config not found")

    core_body: dict = {}
    updates: dict = {}

    if "label" in body:
        label = str(body.get("label") or "").strip()[:80]
        if label:
            core_body["label"] = label
            updates["label"] = label
    if "active" in body:
        core_body["active"] = bool(body.get("active"))
        updates["active"] = core_body["active"]
    if any(k in body for k in ("limit", "limit_bytes", "unit")):
        limit = parse_quota(body)
        core_body["limit_bytes"] = limit or 0
        updates["limit_bytes"] = limit
    if any(k in body for k in ("expiry_days", "expires_at")):
        _days, expires_at = parse_expiry(body)
        core_body["expires_at"] = expires_at.isoformat() if expires_at else None
        updates["expires_at"] = expires_at
    if "speed_mbps" in body:
        speed = parse_speed(body)
        core_body["speed_limit_bytes"] = speed
        updates["speed_limit_bytes"] = speed
    if "ip_limit" in body:
        ip_limit = parse_ip_limit(body)
        core_body["ip_limit"] = ip_limit
        updates["ip_limit"] = ip_limit
    if body.get("reset_usage"):
        core_body["reset_usage"] = True

    if core_body:
        await _core(pool, instance_id, "PATCH", f"links/{link_uuid}", json_body=core_body)

    sets, args = [], []
    for column, value in updates.items():
        args.append(value)
        sets.append(f"{column} = ${len(args)}")
    if body.get("reset_usage"):
        sets.append("used_cache = 0")
    if sets:
        args.extend([instance_id, link_uuid])
        await pool.execute(
            f"UPDATE instance_links SET {', '.join(sets)} "
            f"WHERE instance_id = ${len(args) - 1} AND link_uuid = ${len(args)}",
            *args,
        )
    if user_id and core_body:
        await _activity(pool, user_id, instance_id,
                        f"Config '{row['label'] or link_uuid[:8]}' updated")
    fresh = await pool.fetchrow(
        "SELECT * FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
        instance_id, link_uuid,
    )
    return _link_out(fresh)


async def delete_link(pool, instance_id: str, link_uuid: str, *,
                      user_id: str | None = None, inst_name: str = "") -> None:
    row = await pool.fetchrow(
        "SELECT label FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
        instance_id, link_uuid,
    )
    if row is None:
        raise NotFoundError("config not found")
    try:
        await _core(pool, instance_id, "DELETE", f"links/{link_uuid}")
    except worker_svc.WorkerError:
        log.warning("core delete failed for %s — removing DB row regardless", link_uuid[:8])
    await pool.execute(
        "DELETE FROM instance_links WHERE instance_id = $1 AND link_uuid = $2",
        instance_id, link_uuid,
    )
    if user_id:
        await _activity(pool, user_id, instance_id,
                        f"Config '{row['label'] or link_uuid[:8]}' deleted", level="warn")


async def reset_usage(pool, instance_id: str, link_uuid: str, *,
                      user_id: str | None = None, inst_name: str = "") -> dict:
    """Fresh accounting period for one config (Core counter + DB cache)."""
    return await update_link(pool, instance_id, link_uuid, {"reset_usage": True},
                             user_id=user_id, inst_name=inst_name)


async def reconcile_after_deploy(pool, instance_id: str, default_policy: dict | None) -> None:
    """After a deploy/redeploy, make Core reality match the Console DB:

    * adopt nothing — links created by _provision_default_link are already
      recorded with their policy;
    * re-push any DB row the Core lost (recreated service, wiped state file,
      fresh volume) so configs survive with the same UUIDs and quotas;
    * rows whose Core link exists but policy drifted (e.g. edited while the
      instance was stopped) are re-applied.
    """
    rows = await pool.fetch(
        "SELECT * FROM instance_links WHERE instance_id = $1 ORDER BY created_at",
        instance_id,
    )
    try:
        resp = await _core(pool, instance_id, "GET", "links", timeout=15.0)
        core_links = {l["uuid"]: l for l in resp.get("links", [])}
    except (worker_svc.WorkerError, OSError) as exc:
        log.warning("reconcile for %s skipped (core unreachable): %s", instance_id[:8], exc)
        return
    for row in rows:
        uuid = row["link_uuid"]
        live = core_links.get(uuid)
        policy = {
            "limit_bytes": int(row["limit_bytes"] or 0),
            "expires_at": _iso(row["expires_at"]),
            "speed_limit_bytes": int(row["speed_limit_bytes"] or 0),
            "ip_limit": int(row["ip_limit"] or 0),
        }
        if live is None:
            try:
                await _core(pool, instance_id, "POST", "links", json_body={
                    "uuid": uuid,
                    "label": row["label"] or f"Config {row['protocol'] or ''}".strip(),
                    "protocol": row["protocol"] or "vless-ws",
                    **_policy_to_core(policy),
                    "used_bytes": int(row["used_cache"] or 0),
                }, timeout=30.0)
                log.info("restored lost core link %s for %s", uuid[:8], instance_id[:8])
            except (worker_svc.WorkerError, OSError) as exc:
                log.warning("could not restore core link %s: %s", uuid[:8], exc)
            continue
        drift = (
            int(live.get("limit_bytes") or 0) != policy["limit_bytes"]
            or (live.get("expires_at") or None) != (policy["expires_at"] or None)
            or int(live.get("speed_limit_bytes") or 0) != policy["speed_limit_bytes"]
            or int(live.get("ip_limit") or 0) != policy["ip_limit"]
            or bool(live.get("active", True)) != bool(row["active"])
        )
        if drift:
            try:
                await _core(pool, instance_id, "PATCH", f"links/{uuid}", json_body={
                    **_policy_to_core(policy), "active": bool(row["active"]),
                })
                log.info("re-applied policy for core link %s", uuid[:8])
            except (worker_svc.WorkerError, OSError) as exc:
                log.warning("could not re-apply policy for %s: %s", uuid[:8], exc)


def parse_wizard_policy(config_body: dict) -> dict | None:
    """Wizard → {link_policy} JSON for instance_configs. None when the user
    left everything empty (Default — unlimited, the exact old behavior)."""
    try:
        policy = parse_link_policy(config_body)
    except ValidationError:
        raise
    if not (policy["limit_bytes"] or policy["expires_at"]
            or policy["speed_limit_bytes"] or policy["ip_limit"]):
        return None
    return {
        "limit_bytes": policy["limit_bytes"] or 0,
        "expires_at": policy["expires_at"],
        "speed_limit_bytes": policy["speed_limit_bytes"],
        "ip_limit": policy["ip_limit"],
    }


async def aggregate_quota(pool, instance_id: str, used_bytes: int) -> dict | None:
    """Subscription-level quota when the instance has no volume/time
    envelope: sum of the per-config caps, earliest expiry, real usage
    (AHB group-subscription semantics). None when no config sets anything —
    the subscription then stays honestly Unlimited."""
    rows = await pool.fetch(
        "SELECT limit_bytes, expires_at FROM instance_links "
        "WHERE instance_id = $1 AND active = TRUE",   # TRUE: boolean on PG, integer 1 on SQLite
        instance_id,
    )
    total = sum(int(r["limit_bytes"] or 0) for r in rows)
    expiries = [r["expires_at"] for r in rows if r["expires_at"]]
    if not total and not expiries:
        return None
    expires_at = None
    seconds_remaining = None
    if expiries:
        dates = []
        for value in expiries:
            try:
                dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dates.append(dt)
            except ValueError:
                continue
        if dates:
            expires_at = min(dates)
            seconds_remaining = max(0.0, (expires_at - _utcnow()).total_seconds())
    out = {
        "instance_id": instance_id,
        "limit_bytes": total or None,
        "unlimited": not total,
        "used_bytes": int(used_bytes or 0),
        "live": True,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expired": bool(seconds_remaining is not None and seconds_remaining <= 0),
        "seconds_remaining": seconds_remaining,
        "time_limit_days": None,
    }
    if total:
        out["remaining_bytes"] = max(0, total - int(used_bytes or 0))
        out["percent"] = round(min(100.0, int(used_bytes or 0) / total * 100.0), 1)
        out["exceeded"] = int(used_bytes or 0) >= total
    else:
        out["remaining_bytes"] = None
        out["percent"] = None
        out["exceeded"] = False
    return out


def _fmt(n: int) -> str:
    gb = n / 1024 ** 3
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = n / 1024 ** 2
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{n / 1024:.0f} KB"


async def _activity(pool, user_id, instance_id, message, level="info") -> None:
    try:
        await pool.execute(
            "INSERT INTO activity_events (user_id, instance_id, kind, level, message, created_at) "
            "VALUES ($1, $2, 'config', $3, $4, $5)",
            user_id, instance_id, level, message, _utcnow(),
        )
    except Exception:
        pass  # auditing must never break the action
