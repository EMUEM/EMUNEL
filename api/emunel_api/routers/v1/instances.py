"""EMUNEL API — Instances router.

The Instance is the unit of isolation: own Core subprocess, own port, own
state file, own management token, own links. The protocol/transport matrix
is validated against the exact set the Core supports — no invented
combinations. Secrets (core_api_token, ss_password) never appear in any
response.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.instance import Instance, InstanceStatus, Link
from ...models.user import User
from ...services.auth import get_current_user, require_admin
from ...services.core_client import CoreClient, CoreUnavailable
from ...services.instance_manager import DriverError, get_manager
from ...services import link_sync

router = APIRouter()


# ---------------------------------------------------------------------------
# Protocol / transport / security matrix — mirrors the Core exactly
# ---------------------------------------------------------------------------

PROTOCOL_MATRIX = [
    {"id": "vless-ws", "protocol": "vless", "transport": "websocket",
     "security": "tls", "endpoint": "/ws/{uuid}"},
    {"id": "xhttp-packet-up", "protocol": "vless", "transport": "xhttp",
     "mode": "packet-up", "security": "tls", "endpoint": "/xhttp-siz10/packet-up/{uuid}/{session}/{seq}"},
    {"id": "xhttp-stream-up", "protocol": "vless", "transport": "xhttp",
     "mode": "stream-up", "security": "tls", "endpoint": "/xhttp-siz10/stream-up/{uuid}/{session}"},
    {"id": "trojan-ws", "protocol": "trojan", "transport": "websocket",
     "security": "tls", "endpoint": "/trojan-ws"},
    {"id": "trojan-xhttp-packet-up", "protocol": "trojan", "transport": "xhttp",
     "mode": "packet-up", "security": "tls", "endpoint": "/txhttp-siz10/packet-up/{uuid}/{session}/{seq}"},
    {"id": "trojan-xhttp-stream-up", "protocol": "trojan", "transport": "xhttp",
     "mode": "stream-up", "security": "tls", "endpoint": "/txhttp-siz10/stream-up/{uuid}/{session}"},
    {"id": "shadowsocks", "protocol": "shadowsocks", "transport": "websocket",
     "security": "tls+aead", "endpoint": "/ss-ws"},
    {"id": "vmess-ws", "protocol": "vmess", "transport": "websocket",
     "security": "tls+aead", "endpoint": "/vmess-ws/{uuid}", "requires_xray": True},
]
VALID_PROTOCOL_IDS = {p["id"] for p in PROTOCOL_MATRIX}
DEFAULT_PROTOCOL = "vless-ws"


@router.get("/meta/protocols")
async def protocol_matrix(_: User = Depends(get_current_user)):
    """The exact protocol/transport/security combinations the Core supports."""
    return {"protocols": PROTOCOL_MATRIX, "default": DEFAULT_PROTOCOL}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProtocolConfig(BaseModel):
    enabled: List[str] = Field(default_factory=lambda: [DEFAULT_PROTOCOL])
    default: str = DEFAULT_PROTOCOL


class InstanceCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=60)
    region: str = Field(default="local", max_length=64)
    protocols: Optional[ProtocolConfig] = None
    cpu_limit: float = Field(default=0.5, ge=0.1, le=8)
    memory_mb: int = Field(default=256, ge=128, le=8192)
    max_processes: int = Field(default=128, ge=16, le=1024)
    public_host: Optional[str] = Field(default=None, max_length=255)
    start: bool = Field(default=True)


class InstanceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=60)
    region: Optional[str] = Field(default=None, max_length=64)
    protocols: Optional[ProtocolConfig] = None
    cpu_limit: Optional[float] = Field(default=None, ge=0.1, le=8)
    memory_mb: Optional[int] = Field(default=None, ge=128, le=8192)
    max_processes: Optional[int] = Field(default=None, ge=16, le=1024)
    public_host: Optional[str] = None


def _validate_protocols(cfg: Optional[ProtocolConfig]) -> dict:
    if cfg is None:
        return {"enabled": [DEFAULT_PROTOCOL], "default": DEFAULT_PROTOCOL}
    enabled = [p for p in cfg.enabled if p in VALID_PROTOCOL_IDS]
    if not enabled:
        raise HTTPException(400, detail=f"protocols.enabled must contain at least one of {sorted(VALID_PROTOCOL_IDS)}")
    default = cfg.default if cfg.default in enabled else enabled[0]
    return {"enabled": enabled, "default": default}


def _instance_out(inst: Instance, live: Optional[dict] = None) -> dict:
    data = {
        "id": inst.id,
        "name": inst.name,
        "region": inst.region,
        "status": inst.status.value if isinstance(inst.status, InstanceStatus) else str(inst.status),
        "protocols": inst.protocols or {"enabled": [DEFAULT_PROTOCOL], "default": DEFAULT_PROTOCOL},
        "cpu_limit": inst.cpu_limit,
        "memory_mb": inst.memory_mb,
        "max_processes": inst.max_processes,
        "public_host": inst.public_host,
        "last_error": inst.last_error,
        "link_count": len(inst.links) if inst.links is not None else None,
        "created_at": inst.created_at.isoformat() if inst.created_at else None,
        "started_at": inst.started_at.isoformat() if inst.started_at else None,
        "last_active_at": inst.last_active_at.isoformat() if inst.last_active_at else None,
        # core_api_token intentionally absent
    }
    if live:
        data["live"] = live
    return data


async def _owned_instance(db: AsyncSession, instance_id: str) -> Instance:
    inst = (await db.execute(select(Instance).where(Instance.id == instance_id))).scalar_one_or_none()
    if inst is None:
        raise HTTPException(404, detail="instance not found")
    return inst


def _core(inst: Instance) -> CoreClient:
    if not inst.core_port:
        raise HTTPException(409, detail="instance is not running")
    return CoreClient(inst.core_port, inst.core_api_token)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_instances(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    mgr = get_manager()
    out = []
    for inst in (await db.execute(select(Instance).order_by(Instance.created_at.desc()))).scalars():
        live = None
        try:
            live = await mgr.status(inst.id)
        except Exception:
            pass
        out.append(_instance_out(inst, live))
    return {"instances": out}


@router.post("", status_code=201)
async def create_instance(
    body: InstanceCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    protocols = _validate_protocols(body.protocols)
    inst = Instance(
        name=body.name,
        region=body.region,
        protocols=protocols,
        cpu_limit=body.cpu_limit,
        memory_mb=body.memory_mb,
        max_processes=body.max_processes,
        public_host=body.public_host,
        core_api_token=secrets.token_urlsafe(32),
    )
    db.add(inst)
    await db.commit()
    await db.refresh(inst)

    if body.start:
        await _start_instance(db, inst)
    return _instance_out(inst)


@router.get("/{instance_id}")
async def get_instance(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    inst = await _owned_instance(db, instance_id)
    live = stats = None
    try:
        mgr = get_manager()
        live = await mgr.status(instance_id)
        if live.get("running"):
            stats = await _core(inst).stats()
    except (CoreUnavailable, Exception):
        pass
    return _instance_out(inst, {"driver": live, "core_stats": stats})


@router.patch("/{instance_id}")
async def update_instance(
    instance_id: str,
    body: InstanceUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    if body.name is not None:
        inst.name = body.name
    if body.region is not None:
        inst.region = body.region
    if body.protocols is not None:
        inst.protocols = _validate_protocols(body.protocols)
    if body.cpu_limit is not None:
        inst.cpu_limit = body.cpu_limit
    if body.memory_mb is not None:
        inst.memory_mb = body.memory_mb
    if body.max_processes is not None:
        inst.max_processes = body.max_processes
    if body.public_host is not None:
        inst.public_host = body.public_host or None
    await db.commit()
    return _instance_out(inst)


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    try:
        await get_manager().remove(instance_id)
    except DriverError as exc:
        raise HTTPException(502, detail=str(exc))
    await db.delete(inst)
    await db.commit()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _start_instance(db: AsyncSession, inst: Instance) -> None:
    mgr = get_manager()
    inst.status = InstanceStatus.STARTING
    await db.commit()
    spec = {
        "instance_id": inst.id,
        "name": inst.name,
        "protocols": inst.protocols,
        "api_token": inst.core_api_token,
        "public_host": inst.public_host or "",
        "cpu_limit": inst.cpu_limit,
        "memory_mb": inst.memory_mb,
        "max_processes": inst.max_processes,
    }
    try:
        handle = await mgr.launch(spec)
    except DriverError as exc:
        inst.status = InstanceStatus.FAILED
        inst.last_error = str(exc)
        await db.commit()
        raise HTTPException(502, detail=f"start failed: {exc}")

    inst.core_port = handle["port"]
    inst.core_pid = handle["pid"]
    inst.status = InstanceStatus.RUNNING
    inst.started_at = datetime.now(timezone.utc)
    inst.last_error = None
    await db.commit()

    # re-push all known links into the fresh Core (state file usually covers
    # this, but a wiped state dir must not silently drop credentials)
    links = (await db.execute(select(Link).where(Link.instance_id == inst.id))).scalars().all()
    for link in links:
        await link_sync.push_link(db, link)


@router.post("/{instance_id}/start")
async def start_instance(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    await _start_instance(db, inst)
    return {"ok": True, "status": inst.status.value}


@router.post("/{instance_id}/stop")
async def stop_instance(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    await get_manager().stop(instance_id)
    inst.status = InstanceStatus.STOPPED
    inst.core_port = None
    inst.core_pid = None
    await db.commit()
    return {"ok": True, "status": inst.status.value}


@router.post("/{instance_id}/restart")
async def restart_instance(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    await get_manager().stop(instance_id)
    await _start_instance(db, inst)
    return {"ok": True, "status": inst.status.value}


# ---------------------------------------------------------------------------
# Live core data (real, from the running Core)
# ---------------------------------------------------------------------------

@router.get("/{instance_id}/stats")
async def instance_stats(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    inst = await _owned_instance(db, instance_id)
    try:
        return await _core(inst).stats()
    except CoreUnavailable as exc:
        raise HTTPException(503, detail=str(exc))


@router.get("/{instance_id}/connections")
async def instance_connections(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    inst = await _owned_instance(db, instance_id)
    try:
        return await _core(inst).connections()
    except CoreUnavailable as exc:
        raise HTTPException(503, detail=str(exc))


@router.get("/{instance_id}/logs")
async def instance_logs(
    instance_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    try:
        logs = await _core(inst).logs(limit)
        return {"logs": logs}
    except CoreUnavailable as exc:
        raise HTTPException(503, detail=str(exc))


@router.get("/{instance_id}/metrics")
async def instance_metrics(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    try:
        return await _core(inst).metrics()
    except CoreUnavailable as exc:
        raise HTTPException(503, detail=str(exc))


# ---------------------------------------------------------------------------
# Links (credentials inside the instance)
# ---------------------------------------------------------------------------

class LinkCreate(BaseModel):
    label: str = Field(default="Link", max_length=80)
    protocol: str = DEFAULT_PROTOCOL
    active: bool = True
    limit_bytes: int = Field(default=0, ge=0)  # 0 = unlimited
    expires_at: Optional[datetime] = None
    note: str = Field(default="", max_length=300)
    subscription_id: Optional[str] = None


class LinkUpdate(BaseModel):
    label: Optional[str] = Field(default=None, max_length=80)
    active: Optional[bool] = None
    limit_bytes: Optional[int] = Field(default=None, ge=0)
    expires_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=300)
    reset_usage: bool = False


def _link_out(link: Link) -> dict:
    return {
        "id": link.id,
        "instance_id": link.instance_id,
        "uuid": link.uuid,
        "label": link.label,
        "protocol": link.protocol,
        "active": link.active,
        "limit_bytes": link.limit_bytes,
        "used_bytes": link.used_bytes,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "note": link.note,
        "subscription_id": link.subscription_id,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        # ss_password intentionally absent
    }


@router.get("/{instance_id}/links")
async def list_links(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    inst = await _owned_instance(db, instance_id)
    enabled = (inst.protocols or {}).get("enabled") or [DEFAULT_PROTOCOL]
    links = (await db.execute(select(Link).where(Link.instance_id == inst.id))).scalars().all()
    return {"links": [_link_out(l) for l in links], "enabled_protocols": enabled}


@router.post("/{instance_id}/links", status_code=201)
async def create_link(
    instance_id: str,
    body: LinkCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    inst = await _owned_instance(db, instance_id)
    enabled = (inst.protocols or {}).get("enabled") or [DEFAULT_PROTOCOL]
    if body.protocol not in VALID_PROTOCOL_IDS:
        raise HTTPException(400, detail=f"unsupported protocol: {body.protocol}")
    if body.protocol not in enabled:
        raise HTTPException(400, detail=f"protocol {body.protocol} is not enabled on this instance")
    if body.protocol == "vmess-ws":
        raise HTTPException(400, detail="vmess-ws requires a pinned Xray runtime; configure the Core host first")

    link = Link(
        instance_id=inst.id,
        label=body.label,
        protocol=body.protocol,
        active=body.active,
        limit_bytes=body.limit_bytes,
        expires_at=body.expires_at,
        note=body.note,
        subscription_id=body.subscription_id,
        ss_cipher="chacha20-ietf-poly1305" if body.protocol == "shadowsocks" else None,
        ss_password=secrets.token_urlsafe(16) if body.protocol == "shadowsocks" else None,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)

    if inst.status == InstanceStatus.RUNNING:
        await link_sync.push_link(db, link)
    return _link_out(link)


@router.patch("/{instance_id}/links/{link_id}")
async def update_link(
    instance_id: str,
    link_id: str,
    body: LinkUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    link = (await db.execute(
        select(Link).where(Link.id == link_id, Link.instance_id == instance_id)
    )).scalar_one_or_none()
    if link is None:
        raise HTTPException(404, detail="link not found")

    if body.label is not None:
        link.label = body.label
    if body.active is not None:
        link.active = body.active
    if body.limit_bytes is not None:
        link.limit_bytes = body.limit_bytes
    if body.expires_at is not None:
        link.expires_at = body.expires_at
    if body.note is not None:
        link.note = body.note
    if body.reset_usage:
        link.used_bytes = 0
        if link.instance and link.instance.status == InstanceStatus.RUNNING:
            try:
                await _core(link.instance).update_link(link.uuid, {"reset_usage": True})
            except CoreUnavailable:
                pass
    await db.commit()
    await db.refresh(link)

    if link.instance and link.instance.status == InstanceStatus.RUNNING:
        patch = {}
        if body.label is not None:
            patch["label"] = link.label
        if body.active is not None:
            patch["active"] = link.active
        if body.limit_bytes is not None:
            patch["limit_bytes"] = link.limit_bytes
        if body.expires_at is not None:
            patch["expires_at"] = link.expires_at.isoformat() if link.expires_at else None
        if patch:
            try:
                await _core(link.instance).update_link(link.uuid, patch)
            except CoreUnavailable:
                pass
    return _link_out(link)


@router.delete("/{instance_id}/links/{link_id}", status_code=204)
async def delete_link(
    instance_id: str,
    link_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    link = (await db.execute(
        select(Link).where(Link.id == link_id, Link.instance_id == instance_id)
    )).scalar_one_or_none()
    if link is None:
        raise HTTPException(404, detail="link not found")
    await link_sync.delete_link_from_core(link)
    await db.delete(link)
    await db.commit()


@router.post("/{instance_id}/links/{link_id}/revoke")
async def revoke_link(
    instance_id: str,
    link_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    link = (await db.execute(
        select(Link).where(Link.id == link_id, Link.instance_id == instance_id)
    )).scalar_one_or_none()
    if link is None:
        raise HTTPException(404, detail="link not found")
    link.active = False
    await db.commit()
    await link_sync.revoke_link(db, link)
    return _link_out(link)


@router.get("/{instance_id}/share")
async def share_links(
    instance_id: str,
    host: str = Query(..., max_length=255),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Render client import URLs for all allowed links of an instance."""
    inst = await _owned_instance(db, instance_id)
    if inst.status != InstanceStatus.RUNNING:
        raise HTTPException(409, detail="instance is not running")
    try:
        links = await _core(inst).share_links(host)
        return {"links": links, "host": host}
    except CoreUnavailable as exc:
        raise HTTPException(503, detail=str(exc))
