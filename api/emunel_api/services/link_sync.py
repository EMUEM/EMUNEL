"""EMUNEL LinkSync — the bridge between the Console DB and Core runtimes.

Design rules (from the reference architecture):

* The Core is the enforcement point. Quota, expiry, active flags are pushed
  to the Core's link registry; the Core refuses unknown/disabled/expired/
  over-quota credentials fail-closed. The Console never second-guesses a
  relay decision.
* Traffic accounting is batched: the Core already aggregates per-link bytes
  (EWMA QuotaGate, debounced persistence). The Console polls at a low
  frequency (default 10s) and writes only changed rows.
* Subscription policy is derived onto its links: traffic_limit_bytes,
  expires_at, active. Changes propagate within one poll interval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update, func, and_

from ..models.instance import Instance, InstanceStatus, Link
from ..models.subscription import Subscription
from .core_client import CoreClient, CoreUnavailable
from .instance_manager import get_manager

logger = logging.getLogger("emunel.api.link_sync")

POLL_INTERVAL = float(__import__("os").environ.get("EMUNEL_SYNC_INTERVAL", "10"))
SNAPSHOT_EVERY = 6  # write an analytics snapshot every N polls (60s at default)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# ---------------------------------------------------------------------------
# Link -> Core sync primitives
# ---------------------------------------------------------------------------

def _core_link_body(link: Link, subscription: Optional[Subscription] = None) -> dict:
    """Compose the Core link payload from DB state.

    Quota precedence: an explicit per-link limit wins; otherwise the bound
    subscription's quota applies; 0 = unlimited.
    """
    limit_bytes = link.limit_bytes or 0
    expires_at = link.expires_at
    active = link.active
    if subscription is not None:
        if link.limit_bytes == 0:
            limit_bytes = subscription.traffic_limit_bytes or 0
        if link.expires_at is None and subscription.expires_at is not None:
            expires_at = subscription.expires_at
        if not subscription.is_active:
            active = False  # revoked/disabled subscription revokes its links
    return {
        "uuid": link.uuid,
        "label": link.label or "Link",
        "protocol": link.protocol,
        "active": active,
        "limit_bytes": int(limit_bytes),
        "expires_at": _iso(expires_at) if expires_at else None,
        "note": link.note or "",
        **({"ss_cipher": link.ss_cipher, "ss_password": link.ss_password}
           if link.protocol == "shadowsocks" else {}),
    }


async def push_link(db, link: Link) -> None:
    """Create-or-update the link inside its instance's Core."""
    instance = link.instance
    if instance is None or instance.status != InstanceStatus.RUNNING:
        return
    client = CoreClient(instance.core_port, instance.core_api_token)
    sub = link.subscription
    body = _core_link_body(link, sub)
    try:
        # Core PATCH is idempotent for existing links; create when 404.
        existing = {l["uuid"]: l for l in await client.list_links()}
        if link.uuid in existing:
            patch = {k: v for k, v in body.items() if k != "uuid"}
            await client.update_link(link.uuid, patch)
        else:
            await client.create_link(body)
    except CoreUnavailable as exc:
        logger.warning("push_link failed for %s: %s", link.uuid[:8], exc)


async def revoke_link(db, link: Link) -> None:
    instance = link.instance
    if instance is None or instance.status != InstanceStatus.RUNNING:
        return
    try:
        client = CoreClient(instance.core_port, instance.core_api_token)
        await client.update_link(link.uuid, {"active": False})
    except CoreUnavailable as exc:
        logger.warning("revoke_link failed for %s: %s", link.uuid[:8], exc)


async def delete_link_from_core(link: Link) -> None:
    instance = link.instance
    if instance is None or instance.status != InstanceStatus.RUNNING:
        return
    try:
        client = CoreClient(instance.core_port, instance.core_api_token)
        await client.delete_link(link.uuid)
    except CoreUnavailable as exc:
        logger.warning("delete_link failed for %s: %s", link.uuid[:8], exc)


# ---------------------------------------------------------------------------
# Subscription policy propagation
# ---------------------------------------------------------------------------

async def apply_subscription_policy(db, subscription: Subscription) -> None:
    """Push the subscription's policy onto every bound link."""
    for link in subscription.links:
        await push_link(db, link)


# ---------------------------------------------------------------------------
# Poller: pull counters, mirror status, act on transitions
# ---------------------------------------------------------------------------

async def _poll_instance(db, instance: Instance, tick: int) -> dict:
    client = CoreClient(instance.core_port, instance.core_api_token)
    summary = {"instance_id": instance.id, "ok": False}

    try:
        links = await client.list_links()
        stats = await client.stats()
    except CoreUnavailable as exc:
        summary["error"] = str(exc)
        instance.status = InstanceStatus.DEGRADED
        instance.last_error = f"core unreachable: {exc}"
        return summary

    summary["ok"] = True
    instance.last_error = None
    instance.last_active_at = _utcnow()
    if instance.status == InstanceStatus.DEGRADED:
        instance.status = InstanceStatus.RUNNING

    # --- update link counters (only changed rows) --------------------------
    core_links = {l.get("uuid"): l for l in links}
    db_links = (await db.execute(
        select(Link).where(Link.instance_id == instance.id)
    )).scalars().all()

    for link in db_links:
        core_link = core_links.get(link.uuid)
        if core_link is None:
            # Core lost the credential (state file replaced?) — re-push it.
            await push_link(db, link)
            continue
        used = int(core_link.get("used_bytes") or 0)
        if used != link.used_bytes:
            link.used_bytes = used
            summary.setdefault("changed_links", 0)
            summary["changed_links"] += 1

    summary["total_bytes"] = int(stats.get("total_bytes") or 0)
    summary["active_connections"] = int(stats.get("active_connections") or 0)
    summary["links"] = len(links)
    return summary


async def _poll_subscriptions(db, instance: Instance, connections_by_ip: int) -> None:
    """Recompute subscription aggregates + act on ACTIVE->terminal transitions."""
    subs = (await db.execute(select(Subscription))).scalars().all()
    links_by_sub: dict[str, list[Link]] = {}
    for sub in subs:
        links_by_sub[sub.id] = [l for l in sub.links if l.instance_id == instance.id]

    for sub in subs:
        sub_links = links_by_sub.get(sub.id) or []
        if not sub_links:
            continue
        sub.traffic_used_bytes = sum(l.used_bytes for l in sub_links if l.instance_id == instance.id)
        # distinct source IPs currently connected on this instance approximate
        # "active devices"; the Core reports grouped-by-IP connections.
        sub.active_devices = connections_by_ip if connections_by_ip else 0

        was_active = sub.is_active
        # monthly reset (policy: reset_day, at most once per day-of-month)
        if sub.reset_day and _should_monthly_reset(sub):
            for link in sub_links:
                link.used_bytes = 0
                try:
                    client = CoreClient(instance.core_port, instance.core_api_token)
                    await client.update_link(link.uuid, {"reset_usage": True})
                except CoreUnavailable:
                    pass
            sub.traffic_used_bytes = 0
            sub.last_reset_at = _utcnow()

        # ACTIVE -> EXPIRED / QUOTA_EXCEEDED transitions (auto_disable)
        if was_active and sub.is_expired:
            sub.is_active = False
        if was_active and sub.auto_disable and sub.is_quota_exceeded:
            sub.is_active = False
        # propagate revocation to the Core so active tunnels die
        if was_active and not sub.is_active:
            for link in sub_links:
                await revoke_link(db, link)


def _should_monthly_reset(sub: Subscription) -> bool:
    now = _utcnow()
    if now.day != sub.reset_day:
        return False
    if sub.last_reset_at is None:
        return True
    last = sub.last_reset_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(days=1)


async def poll_once(db) -> dict:
    """One synchronization pass across all running instances."""
    result = {"instances": 0, "ok": 0, "degraded": 0, "changed_links": 0}
    running = (await db.execute(
        select(Instance).where(Instance.status.in_([InstanceStatus.RUNNING, InstanceStatus.DEGRADED]))
    )).scalars().all()

    for instance in running:
        summary = await _poll_instance(db, instance, 0)
        result["instances"] += 1
        if summary.get("ok"):
            result["ok"] += 1
            result["changed_links"] += summary.get("changed_links", 0)
            try:
                client = CoreClient(instance.core_port, instance.core_api_token)
                conns = await client.connections()
                await _poll_subscriptions(db, instance, conns.get("count") or 0)
            except CoreUnavailable:
                pass
        else:
            result["degraded"] += 1
    return result


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

class SyncWorker:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_result: dict = {}
        self.last_run: Optional[datetime] = None
        self._tick = 0

    async def run(self, session_factory) -> None:
        while not self._stop.is_set():
            try:
                async with session_factory() as db:
                    self.last_result = await poll_once(db)
                    await db.commit()
                self.last_run = _utcnow()
            except Exception as exc:  # never let the poller die
                logger.warning("sync pass failed: %s", exc)
            self._tick += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    def start(self, session_factory) -> None:
        if self._task is None or self._task.done():
            # recreate the stop event so it binds to the *current* loop
            # (hot reload / test harnesses may switch event loops)
            self._stop = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self.run(session_factory))
            logger.info("link sync worker started (interval=%ss)", POLL_INTERVAL)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


sync_worker = SyncWorker()
