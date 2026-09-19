"""EMUNEL API — Network diagnostics router (real measurements only).

Every result is a real measurement. Latency types are labelled explicitly.
On-demand only, rate limited, concurrency capped.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...models.user import User
from ...services.auth import get_current_user
from ...services import diagnostics as diag

router = APIRouter()


class TcpProbe(BaseModel):
    host: str = Field(..., max_length=255)
    port: int = Field(..., ge=1, le=65535)


class TlsProbe(BaseModel):
    host: str = Field(..., max_length=255)
    port: int = Field(default=443, ge=1, le=65535)
    verify: bool = False
    server_name: Optional[str] = None


class HttpProbe(BaseModel):
    url: str = Field(..., max_length=2048)
    method: str = Field(default="HEAD")


class WsProbe(BaseModel):
    url: str = Field(..., max_length=2048)


class XhttpProbe(BaseModel):
    base_url: str = Field(..., max_length=2048)
    path: str = Field(default="/xhttp-siz10/packet-up/00000000-0000-0000-0000-000000000000/test/0", max_length=1024)


def _rate_limited(user: User) -> None:
    if not diag.check_rate_limit(user.id):
        raise HTTPException(429, detail="diagnostic rate limit exceeded — retry shortly")


@router.get("/capabilities")
async def capabilities(_: User = Depends(get_current_user)):
    """What the diagnostics subsystem can measure (and how it is bounded)."""
    return {
        "probes": [
            {"name": "dns", "measures": "DNS resolution latency"},
            {"name": "tcp", "measures": "TCP connect (3-way handshake) latency"},
            {"name": "tls", "measures": "TLS handshake latency (incl. SNI + cert)"},
            {"name": "http", "measures": "HTTP response latency"},
            {"name": "websocket", "measures": "WS tunnel establishment latency + ping RTT"},
            {"name": "grpc", "measures": "HTTP-layer probe of gRPC-style endpoint"},
            {"name": "xhttp", "measures": "xHTTP transport route reachability"},
            {"name": "chain", "measures": "DNS -> TCP -> TLS -> tunnel staged breakdown"},
        ],
        "latency_types": ["server", "tcp_connect", "tls_handshake", "ws_tunnel", "e2e"],
        "limits": {
            "probe_timeout_s": diag.PROBE_TIMEOUT,
            "max_concurrency": diag.MAX_CONCURRENCY,
            "rate_capacity": diag.RATE_CAPACITY,
            "rate_refill_per_min": diag.RATE_REFILL_PER_MIN,
        },
    }


@router.post("/dns")
async def run_dns(body: TcpProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    return (await diag.run_batch([diag.probe_dns(body.host)]))[0]


@router.post("/tcp")
async def run_tcp(body: TcpProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    return (await diag.run_batch([diag.probe_tcp(body.host, body.port)]))[0]


@router.post("/tls")
async def run_tls(body: TlsProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    return (await diag.run_batch([
        diag.probe_tls(body.host, body.port, verify=body.verify, server_name=body.server_name)
    ]))[0]


@router.post("/http")
async def run_http(body: HttpProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    if body.method not in ("HEAD", "GET"):
        raise HTTPException(400, detail="method must be HEAD or GET")
    return (await diag.run_batch([diag.probe_http(body.url, method=body.method)]))[0]


@router.post("/websocket")
async def run_websocket(body: WsProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    if not body.url.startswith(("ws://", "wss://")):
        raise HTTPException(400, detail="url must be ws:// or wss://")
    return (await diag.run_batch([diag.probe_websocket(body.url)]))[0]


@router.post("/grpc")
async def run_grpc(body: HttpProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    return (await diag.run_batch([diag.probe_grpc_style(body.url, "/")]))[0]


@router.post("/xhttp")
async def run_xhttp(body: XhttpProbe, user: User = Depends(get_current_user)):
    _rate_limited(user)
    return (await diag.run_batch([diag.probe_xhttp(body.base_url, body.path)]))[0]


class ChainProbe(BaseModel):
    ws_url: str = Field(..., max_length=2048)


@router.post("/chain")
async def run_chain(body: ChainProbe, user: User = Depends(get_current_user)):
    """Staged breakdown: DNS -> TCP -> (TLS) -> WS tunnel establishment."""
    _rate_limited(user)
    return await diag.probe_tunnel_chain(body.ws_url, api_base="")


@router.post("/batch")
async def run_batch(body: TcpProbe, user: User = Depends(get_current_user)):
    """Run DNS + TCP + TLS against one target; every stage labelled."""
    _rate_limited(user)
    results = await diag.run_batch([
        diag.probe_dns(body.host),
        diag.probe_tcp(body.host, body.port),
        diag.probe_tls(body.host, 443 if body.port == 443 else body.port),
    ])
    return {"target": body.host, "stages": results}
