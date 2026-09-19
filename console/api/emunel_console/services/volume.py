"""Per-instance volume (data) and time limits — console-side accounting only.

The EMUNEL Core already counts its lifetime transferred bytes
(RuntimeStats.total_bytes) and persists them across restarts in the
instance state file. This module layers optional operator-set caps on
top of that counter WITHOUT touching the Core:

* the limits live in the Console database (``instance_volume`` table);
  no limit / empty value  →  Default, treated as Unlimited
* ``used = core_lifetime_bytes - baseline`` — the baseline lets the
  operator reset the usage counter for a new billing period
* the last known usage is cached so stopped instances still show it
* when usage reaches the limit — or the time limit passes — the Console
  stops the instance through the normal lifecycle service (exactly what
  the Stop button does)

The real limits are also surfaced to subscribers: ``userinfo_header``()
builds the standard ``subscription-userinfo`` value proxy clients parse
(total / expire / download), so the panel, the subscription page and the
user's client all read the same numbers.

Enforcement is deliberately conservative: it only ever acts on instances
that (a) have a limit set and (b) are currently running, and every step
is wrapped so the Console can never crash-loop because of it.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import asyncpg

from ..config import settings
from ..logging import get
from . import deployments as deploy_svc
from . import workers as worker_svc

log = get("runtime", "emunel.console.volume")

MIN_LIMIT_BYTES = 1024 * 1024          # 1 MB
MAX_LIMIT_BYTES = 1024 ** 5           # 1 PB
MIN_TIME_LIMIT_DAYS = 1 / 1440        # one minute
MAX_TIME_LIMIT_DAYS = 3650             # ten years


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """PG TIMESTAMPTZ is aware; defensive guard for naive values."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_limit(body: dict) -> int | None:
    """Accept {limit_gb} (fractional) or {limit_bytes} (raw). Empty/None/0
    means the Default — unlimited."""
    raw_gb = body.get("limit_gb", None)
    raw_b = body.get("limit_bytes", None)
    if isinstance(raw_gb, str) and not raw_gb.strip():
        raw_gb = None
    if isinstance(raw_b, str) and not raw_b.strip():
        raw_b = None
    if raw_gb is None and raw_b is None:
        return None
    if raw_gb is not None:
        try:
            value = float(raw_gb)
        except (TypeError, ValueError):
            raise ValueError("limit_gb must be a number")
        if value < 0:
            raise ValueError("limit_gb cannot be negative — leave it empty for unlimited")
        if value == 0:
            return None
        limit = int(value * 1024 ** 3)
    else:
        try:
            limit = int(raw_b)
        except (TypeError, ValueError):
            raise ValueError("limit_bytes must be an integer")
        if limit < 0:
            raise ValueError("limit_bytes cannot be negative — leave it empty for unlimited")
        if limit == 0:
            return None
    if limit < MIN_LIMIT_BYTES:
        raise ValueError("limit is below the 1 MB minimum")
    if limit > MAX_LIMIT_BYTES:
        raise ValueError("limit is above the 1 PB maximum")
    return limit


def parse_time_limit(body: dict) -> tuple[float | None, datetime | None]:
    """Accept {time_limit_days} (fractional days). Empty/None/0 means the
    Default — unlimited. Returns (days, expires_at); (None, None) clears it."""
    raw = body.get("time_limit_days", None)
    if isinstance(raw, str) and not raw.strip():
        raw = None
    if raw is None:
        return None, None
    try:
        days = float(raw)
    except (TypeError, ValueError):
        raise ValueError("time_limit_days must be a number")
    if days < 0:
        raise ValueError("time_limit_days cannot be negative — leave it empty for unlimited")
    if days == 0:
        return None, None
    if days < MIN_TIME_LIMIT_DAYS:
        raise ValueError("time limit is below the one-minute minimum")
    if days > MAX_TIME_LIMIT_DAYS:
        raise ValueError("time limit is above the ten-year maximum")
    return days, _utcnow() + timedelta(days=days)


async def _row(pool, instance_id: str):
    return await pool.fetchrow(
        "SELECT instance_id, limit_bytes, baseline_bytes, used_cache, used_at, "
        "time_limit_days, expires_at FROM instance_volume WHERE instance_id = $1",
        instance_id,
    )


async def _live_total(pool, instance_id: str) -> int | None:
    """Lifetime transferred bytes as reported by the instance's Core
    (persisted by the Core itself, so it survives restarts)."""
    row = await pool.fetchrow(
        "SELECT node_id FROM deployments WHERE instance_id = $1 "
        "ORDER BY started_at DESC LIMIT 1",
        instance_id,
    )
    node_id = (row["node_id"] if row else None) or settings.default_worker_node
    node_url = worker_svc.worker_url_for(node_id)
    try:
        stats = await worker_svc.worker_call(
            node_url, "GET",
            f"/worker/api/instances/{instance_id}/proxy/core/api/stats",
            timeout=8.0,
        )
    except (worker_svc.WorkerError, OSError):
        return None
    total = stats.get("total_bytes")
    return int(total) if isinstance(total, (int, float)) else None


async def get_state(pool, instance_id: str) -> dict:
    """Current volume/time state for an instance. Live usage when the Core
    is reachable, the cached value otherwise."""
    row = await _row(pool, instance_id)
    limit = int(row["limit_bytes"]) if row and row["limit_bytes"] else None
    baseline = int(row["baseline_bytes"]) if row and row["baseline_bytes"] else 0
    cached = int(row["used_cache"]) if row and row["used_cache"] is not None else 0
    time_days = (float(row["time_limit_days"])
                 if row and row["time_limit_days"] is not None else None)
    expires_at = (row["expires_at"] if row and row["expires_at"] is not None else None)

    total = await _live_total(pool, instance_id)
    live = total is not None
    if live:
        used = max(0, total - baseline)
        await pool.execute(
            "UPDATE instance_volume SET used_cache = $2, used_at = $3 WHERE instance_id = $1",
            instance_id, used, _utcnow(),
        )
    else:
        used = cached

    expired = False
    seconds_remaining = None
    if expires_at is not None:
        expires_dt = _aware(expires_at)
        expires_at = expires_dt
        seconds_remaining = max(0.0, (expires_dt - _utcnow()).total_seconds())
        expired = seconds_remaining <= 0

    out = {
        "instance_id": instance_id,
        "limit_bytes": limit,
        "unlimited": limit is None,
        "used_bytes": used,
        "live": live,
        "baseline_bytes": baseline,
        "used_at": (row["used_at"].isoformat() if row and row["used_at"] is not None else None),
        "time_limit_days": time_days,
        "expires_at": (expires_at.isoformat() if expires_at is not None else None),
        "expired": expired,
        "seconds_remaining": seconds_remaining,
    }
    if limit:
        out["remaining_bytes"] = max(0, limit - used)
        out["percent"] = round(min(100.0, used / limit * 100.0), 1)
        out["exceeded"] = used >= limit
    else:
        out["remaining_bytes"] = None
        out["percent"] = None
        out["exceeded"] = False
    return out


async def set_limit(pool, instance_id: str, limit: int | None,
                    *, user_id: str | None = None, name: str = "") -> None:
    """Upsert the cap. limit=None clears it (Default / unlimited)."""
    now = _utcnow()
    await pool.execute(
        """
        INSERT INTO instance_volume (instance_id, limit_bytes, baseline_bytes,
                                     used_cache, used_at, updated_at)
        VALUES ($1, $2, 0, 0, NULL, $3)
        ON CONFLICT (instance_id) DO UPDATE SET
            limit_bytes = $2, updated_at = $3
        """,
        instance_id, limit, now,
    )
    if user_id:
        await _activity(pool, user_id, instance_id,
                        "Volume limit set to unlimited (default)"
                        if limit is None else f"Volume limit for '{name}' set to {_fmt(limit)}")


async def set_time_limit(pool, instance_id: str, days: float | None,
                         expires_at: datetime | None, *,
                         user_id: str | None = None, name: str = "") -> None:
    """Upsert the time cap. expires_at=None clears it (Default / unlimited).
    Volume columns are never touched — the two limits are independent."""
    now = _utcnow()
    await pool.execute(
        """
        INSERT INTO instance_volume (instance_id, time_limit_days, expires_at,
                                     updated_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (instance_id) DO UPDATE SET
            time_limit_days = $2, expires_at = $3, updated_at = $4
        """,
        instance_id, days, expires_at, now,
    )
    if user_id:
        if expires_at is None:
            message = "Time limit set to unlimited (default)"
        else:
            message = (f"Time limit for '{name}' set to {_fmt_days(days)} — "
                       f"expires {_fmt_when(expires_at)}")
        await _activity(pool, user_id, instance_id, message)


async def reset_usage(pool, instance_id: str, *, user_id: str | None = None,
                      name: str = "") -> None:
    """Start a fresh accounting period: everything transferred so far moves
    below the baseline. Uses the live Core counter when available, otherwise
    the last cached usage."""
    row = await _row(pool, instance_id)
    if row is None:
        await set_limit(pool, instance_id, None)
        return
    baseline = int(row["baseline_bytes"] or 0)
    cached = int(row["used_cache"] or 0)
    total = await _live_total(pool, instance_id)
    if total is not None:
        new_baseline = max(baseline, total)
    else:
        new_baseline = baseline + cached
    await pool.execute(
        "UPDATE instance_volume SET baseline_bytes = $2, used_cache = 0, updated_at = $3 "
        "WHERE instance_id = $1",
        instance_id, new_baseline, _utcnow(),
    )
    if user_id:
        await _activity(pool, user_id, instance_id,
                        f"Usage counter for '{name}' reset")


async def limit_reached(pool, instance_id: str) -> tuple[bool, dict | None]:
    """Fast check used to gate deploys: only consults the stored limit and
    the last cached usage (no worker round-trip)."""
    row = await _row(pool, instance_id)
    if row is None or not row["limit_bytes"]:
        return False, None
    limit = int(row["limit_bytes"])
    used = int(row["used_cache"] or 0)
    if used < limit:
        return False, None
    return True, {"limit_bytes": limit, "used_bytes": used}


async def time_reached(pool, instance_id: str) -> tuple[bool, dict | None]:
    """Fast check used to gate deploys: has the time limit passed?"""
    row = await _row(pool, instance_id)
    if row is None or row["expires_at"] is None:
        return False, None
    expires_at = _aware(row["expires_at"])
    if expires_at > _utcnow():
        return False, None
    return True, {"expires_at": expires_at.isoformat()}


async def enforce_all(pool) -> None:
    """Stop every running instance that has reached its volume or time cap.
    Never raises."""
    try:
        rows = await pool.fetch(
            """
            SELECT iv.instance_id, iv.limit_bytes, iv.baseline_bytes,
                   iv.used_cache, iv.expires_at, i.name
            FROM instance_volume iv
            JOIN instances i ON i.id = iv.instance_id
            WHERE i.status = 'running'
              AND ( (iv.limit_bytes IS NOT NULL AND iv.limit_bytes > 0)
                    OR iv.expires_at IS NOT NULL )
            """
        )
    except Exception as exc:  # schema not migrated yet, DB hiccup, …
        log.warning("volume enforcement skipped: %s", exc)
        return
    for row in rows:
        instance_id = str(row["instance_id"])
        limit = int(row["limit_bytes"]) if row["limit_bytes"] else None
        baseline = int(row["baseline_bytes"] or 0)
        expires_at = row["expires_at"]
        try:
            if expires_at is not None:
                expires_dt = _aware(expires_at)
                if expires_dt <= _utcnow():
                    log.info("time limit reached for %s (expired %s) — stopping",
                             instance_id, expires_dt.isoformat())
                    await deploy_svc.stop_instance(pool, instance_id)
                    owner = await pool.fetchrow(
                        "SELECT user_id FROM instances WHERE id = $1", instance_id)
                    await _activity(
                        pool, str(owner["user_id"]) if owner else None, instance_id,
                        f"Instance '{row['name']}' stopped — time limit reached "
                        f"(expired {_fmt_when(expires_dt)})",
                        level="warn",
                    )
                    continue
            if not limit:
                continue
            total = await _live_total(pool, instance_id)
            if total is None:
                continue
            used = max(0, total - baseline)
            await pool.execute(
                "UPDATE instance_volume SET used_cache = $2, used_at = $3 WHERE instance_id = $1",
                instance_id, used, _utcnow(),
            )
            if used < limit:
                continue
            log.info("volume cap reached for %s (%s of %s) — stopping",
                     instance_id, _fmt(used), _fmt(limit))
            await deploy_svc.stop_instance(pool, instance_id)
            owner = await pool.fetchrow(
                "SELECT user_id FROM instances WHERE id = $1", instance_id)
            await _activity(
                pool, str(owner["user_id"]) if owner else None, instance_id,
                f"Instance '{row['name']}' stopped — volume limit reached "
                f"({_fmt(used)} of {_fmt(limit)})",
                level="warn",
            )
        except Exception as exc:
            log.warning("volume check failed for %s: %s", instance_id, exc)


async def enforcement_loop(pool) -> None:
    interval = max(15.0, float(os.environ.get("EMUNEL_VOLUME_CHECK_SECONDS", "45")))
    log.info("volume enforcement loop started (every %.0fs)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await enforce_all(pool)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # absolute safety net — never die
            log.warning("volume enforcement loop error: %s", exc)


def _fmt(n: int) -> str:
    gb = n / 1024 ** 3
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{n / 1024 ** 2:.1f} MB"


def _fmt_days(days: float | None) -> str:
    if days is None:
        return "unlimited"
    if days >= 1:
        return f"{days:.0f} days"
    return f"{days * 24:.1f} hours"


def _fmt_when(dt: datetime) -> str:
    return _aware(dt).strftime("%Y-%m-%d %H:%M UTC")


def userinfo_header(state: dict | None) -> str:
    """Standard ``subscription-userinfo`` header value proxy clients parse
    (v2rayNG, Streisand, Happ, …): ``upload``/``download`` = bytes used,
    ``total`` = volume cap, ``expire`` = unix timestamp of the time limit.
    0 means unlimited — the convention every client understands — so an
    empty value keeps the exact previous Default behavior."""
    if not state:
        return "upload=0; download=0; total=0; expire=0"
    used = max(0, int(state.get("used_bytes") or 0))
    limit = int(state["limit_bytes"]) if state.get("limit_bytes") else 0
    expire = 0
    if state.get("expires_at"):
        try:
            expire = int(_aware(datetime.fromisoformat(str(state["expires_at"]))).timestamp())
        except ValueError:
            expire = 0
    return f"upload=0; download={used}; total={limit}; expire={expire}"


async def _activity(pool, user_id, instance_id, message, level="info") -> None:
    try:
        await pool.execute(
            "INSERT INTO activity_events (user_id, instance_id, kind, level, message, created_at) "
            "VALUES ($1, $2, 'volume', $3, $4, $5)",
            user_id, instance_id, level, message, _utcnow(),
        )
    except Exception:
        pass  # auditing must never break the action
