"""Sensitive admin configuration export and atomic insert-only import."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..config import settings
from ..db import get_pool
from ..services import backup as service
from .admin import admin_user

router = APIRouter(prefix="/api/admin/backup", tags=["admin-backup"])
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


async def document(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, detail="Send the raw backup as application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, detail="Compressed backups are not supported")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            length = int(length)
        except ValueError:
            raise HTTPException(400, detail="Invalid Content-Length") from None
        if length < 0:
            raise HTTPException(400, detail="Invalid Content-Length")
        if length > service.MAX_BYTES:
            raise HTTPException(413, detail="Backup exceeds 16 MiB limit")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > service.MAX_BYTES:
            raise HTTPException(413, detail="Backup exceeds 16 MiB limit")
        data.extend(chunk)
    return service.parse(bytes(data))


def json_response(value):
    return Response(service.encode(value), media_type="application/json", headers=HEADERS)


@router.get("")
async def capabilities(_=Depends(admin_user)):
    return json_response(service.capabilities())


@router.post("/export")
async def export(request: Request, _=Depends(admin_user)):
    raw = await service.export_backup(get_pool(request), settings.secret_key)
    return Response(raw, media_type="application/json", headers={
        **HEADERS, "Content-Disposition": 'attachment; filename="emunel-configuration-backup-v1.json"',
    })


@router.post("/validate")
async def validate(request: Request, _=Depends(admin_user)):
    doc = await document(request)
    result = await service.restore_backup(get_pool(request), doc, settings.secret_key, dry_run=True)
    return json_response(result)


@router.post("/restore")
async def restore(request: Request, admin=Depends(admin_user)):
    if request.headers.get("x-emunel-backup-confirm") != "import":
        raise HTTPException(400, detail="Explicit confirmation required: X-EMUNEL-Backup-Confirm: import")
    doc = await document(request)
    result = await service.restore_backup(get_pool(request), doc, settings.secret_key,
                                          dry_run=False, actor_id=admin["id"])
    return json_response(result)
