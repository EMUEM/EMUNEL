"""On-demand network diagnostics with bounded timeouts."""

import asyncio
import socket
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ...models.user import User
from ...services.auth import get_current_user

router = APIRouter()

class TcpTestRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=253)
    port: int = Field(..., ge=1, le=65535)
    timeout_seconds: float = Field(3.0, gt=0.1, le=10.0)

@router.post("/tcp")
async def tcp_test(payload: TcpTestRequest, _: User = Depends(get_current_user)):
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(payload.host, payload.port), payload.timeout_seconds
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as exc:
        return {"ok": False, "kind": "tcp", "host": payload.host, "port": payload.port, "error": str(exc)}
    return {"ok": True, "kind": "tcp", "host": payload.host, "port": payload.port, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
