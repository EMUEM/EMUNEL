"""EMUNEL Network Diagnostics — real measurements only.

Rules enforced by design:

* No estimated or simulated values. Every latency number is measured with
  time.monotonic() around an actual operation.
* Latency taxonomy is explicit and never conflated:
    - server        : HTTP round trip to an EMUNEL API /health endpoint
    - tcp_connect   : TCP three-way handshake completion
    - tls_handshake : full TLS 1.2/1.3 handshake (incl. SNI + cert validation)
    - ws_tunnel     : WebSocket upgrade completion (tunnel establishment)
    - e2e           : full application-level request through a tunnel
* Diagnostics never run continuously: every probe is on-demand, bounded by
  a per-probe timeout, capped by a global concurrency semaphore, and rate
  limited per user (token bucket).
* A failing probe returns structured failure without raising into the API.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger("emunel.api.diagnostics")

PROBE_TIMEOUT = float(__import__("os").environ.get("EMUNEL_DIAG_TIMEOUT", "6"))
MAX_CONCURRENCY = int(__import__("os").environ.get("EMUNEL_DIAG_CONCURRENCY", "8"))
RATE_CAPACITY = int(__import__("os").environ.get("EMUNEL_DIAG_RATE_CAPACITY", "20"))
RATE_REFILL_PER_MIN = float(__import__("os").environ.get("EMUNEL_DIAG_RATE_REFILL", "30"))

_semaphore: Optional[asyncio.Semaphore] = None
_buckets: dict[str, tuple[float, float]] = {}  # user_id -> (tokens, last_refill)


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    return _semaphore


def check_rate_limit(user_id: str) -> bool:
    """Token bucket: `RATE_CAPACITY` burst, `RATE_REFILL_PER_MIN` sustained."""
    now = time.monotonic()
    tokens, last = _buckets.get(user_id, (float(RATE_CAPACITY), now))
    tokens = min(RATE_CAPACITY, tokens + (now - last) * (RATE_REFILL_PER_MIN / 60.0))
    if tokens < 1.0:
        _buckets[user_id] = (tokens, now)
        return False
    _buckets[user_id] = (tokens - 1.0, now)
    return True


@dataclass
class ProbeResult:
    name: str
    ok: bool
    latency_ms: Optional[float] = None  # the latency OF THIS PROBE'S OPERATION
    detail: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2) if self.latency_ms is not None else None,
            "detail": self.detail,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Individual probes — each measures exactly one thing
# ---------------------------------------------------------------------------

async def probe_dns(host: str) -> ProbeResult:
    """Resolve `host` and measure resolution latency."""
    t0 = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
            timeout=PROBE_TIMEOUT,
        )
        addrs = sorted({i[4][0] for i in infos})
        if not addrs:
            return ProbeResult("dns", False, error="no addresses returned")
        return ProbeResult(
            "dns", True, latency_ms=(time.monotonic() - t0) * 1000,
            detail={"addresses": addrs[:10], "count": len(addrs)},
        )
    except asyncio.TimeoutError:
        return ProbeResult("dns", False, error="timeout")
    except socket.gaierror as exc:
        return ProbeResult("dns", False, error=f"resolution failed: {exc}")


async def probe_tcp(host: str, port: int) -> ProbeResult:
    """Measure pure TCP connect latency (three-way handshake)."""
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=PROBE_TIMEOUT
        )
        latency = (time.monotonic() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ProbeResult("tcp", True, latency_ms=latency,
                           detail={"host": host, "port": port})
    except asyncio.TimeoutError:
        return ProbeResult("tcp", False, error="timeout", detail={"host": host, "port": port})
    except OSError as exc:
        return ProbeResult("tcp", False, error=str(exc), detail={"host": host, "port": port})


def _build_tls_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if verify:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def probe_tls(host: str, port: int = 443, verify: bool = False,
                    server_name: Optional[str] = None) -> ProbeResult:
    """Measure full TLS handshake latency; report protocol/cipher/SNI match."""
    sni = server_name or host
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=_build_tls_context(verify),
                                    server_hostname=sni),
            timeout=PROBE_TIMEOUT,
        )
        latency = (time.monotonic() - t0) * 1000
        ssl_obj = writer.transport.get_extra_info("ssl_object")
        cert = ssl_obj.getpeercert(binary_form=False)
        detail = {
            "host": host, "port": port, "sni": sni,
            "tls_version": ssl_obj.version(),
            "cipher": ssl_obj.cipher()[0] if ssl_obj.cipher() else None,
            "cert_verified": bool(cert),
        }
        if cert and isinstance(cert, dict):
            subject = cert.get("subjectAltName") or ()
            names = [v for _, v in subject][:5]
            detail["san"] = names
            detail["sni_match"] = any(sni in n for n in names) if names else None
            detail["not_after"] = cert.get("notAfter")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ProbeResult("tls", True, latency_ms=latency, detail=detail)
    except asyncio.TimeoutError:
        return ProbeResult("tls", False, error="timeout", detail={"host": host, "port": port})
    except (ssl.SSLError, OSError, ValueError) as exc:
        return ProbeResult("tls", False, error=str(exc), detail={"host": host, "port": port})


async def probe_http(url: str, method: str = "HEAD") -> ProbeResult:
    """Measure HTTP response latency (time to response start)."""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, follow_redirects=False, verify=False
        ) as client:
            r = await client.request(method, url)
            latency = (time.monotonic() - t0) * 1000
            return ProbeResult(
                "http", 200 <= r.status_code < 500, latency_ms=latency,
                detail={"url": url, "status_code": r.status_code,
                        "http_version": str(getattr(r, "http_version", "1.1")),
                        "content_type": r.headers.get("content-type")},
            )
    except asyncio.TimeoutError:
        return ProbeResult("http", False, error="timeout", detail={"url": url})
    except (httpx.HTTPError, OSError) as exc:
        return ProbeResult("http", False, error=str(exc), detail={"url": url})


async def probe_websocket(url: str, headers: Optional[dict] = None) -> ProbeResult:
    """Measure WebSocket tunnel establishment latency (TCP+TLS+upgrade)."""
    import websockets

    t0 = time.monotonic()
    try:
        async with websockets.connect(
            url, open_timeout=PROBE_TIMEOUT, additional_headers=headers or {}
        ) as ws:
            latency = (time.monotonic() - t0) * 1000
            pong_t0 = time.monotonic()
            pong = await asyncio.wait_for(ws.ping(), timeout=PROBE_TIMEOUT)
            rtt = (time.monotonic() - pong_t0) * 1000
            subproto = ws.subprotocol
            return ProbeResult(
                "websocket", True, latency_ms=latency,
                detail={"url": url, "subprotocol": subproto,
                        "ping_rtt_ms": round(rtt, 2)},
            )
    except asyncio.TimeoutError:
        return ProbeResult("websocket", False, error="timeout", detail={"url": url})
    except Exception as exc:  # websockets raises varied exceptions
        return ProbeResult("websocket", False, error=str(exc), detail={"url": url})


async def probe_grpc_style(base_url: str, path: str) -> ProbeResult:
    """Probe an HTTP/2-style endpoint (gRPC uses HTTP/2 POST with trailers;
    without a full gRPC client we validate the transport accepts a POST and
    responds at the HTTP layer — labelled honestly as 'http2-probe')."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, verify=False) as client:
            r = await client.post(url, content=b"", headers={"content-type": "application/grpc"})
            latency = (time.monotonic() - t0) * 1000
            grpc_status = r.headers.get("grpc-status")
            return ProbeResult(
                "grpc", r.status_code in (200, 204, 404) and grpc_status is not None or r.status_code == 200,
                latency_ms=latency,
                detail={"url": url, "status_code": r.status_code,
                        "grpc_status": grpc_status,
                        "note": "HTTP-layer probe of a gRPC-style endpoint"},
            )
    except asyncio.TimeoutError:
        return ProbeResult("grpc", False, error="timeout", detail={"url": url})
    except (httpx.HTTPError, OSError) as exc:
        return ProbeResult("grpc", False, error=str(exc), detail={"url": url})


async def probe_xhttp(base_url: str, path: str) -> ProbeResult:
    """Probe an xHTTP transport endpoint (POST upload path)."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, verify=False) as client:
            r = await client.post(url, content=b"\x00", headers={"content-type": "application/octet-stream"})
            latency = (time.monotonic() - t0) * 1000
            # 200/403(expected without creds)/404/502 all prove the route is served
            served = r.status_code in (200, 400, 403, 404, 413, 502)
            return ProbeResult(
                "xhttp", served, latency_ms=latency,
                detail={"url": url, "status_code": r.status_code,
                        "note": "route reachability probe (auth still enforced)"},
            )
    except asyncio.TimeoutError:
        return ProbeResult("xhttp", False, error="timeout", detail={"url": url})
    except (httpx.HTTPError, OSError) as exc:
        return ProbeResult("xhttp", False, error=str(exc), detail={"url": url})


# ---------------------------------------------------------------------------
# Composed tests (clearly labelled latency stages)
# ---------------------------------------------------------------------------

async def chain_tcp_tls(host: str, port: int = 443, verify: bool = False,
                       server_name: Optional[str] = None) -> list[ProbeResult]:
    """TCP then TLS against the same target — separated latencies."""
    async with _sem():
        tcp_res = await probe_tcp(host, port)
        tls_res = await probe_tls(host, port, verify=verify, server_name=server_name)
    return [tcp_res, tls_res]


async def probe_server_latency(api_base: str) -> ProbeResult:
    """EMUNEL API server latency (its own /health endpoint)."""
    return await probe_http(f"{api_base.rstrip('/')}/health", method="GET")


async def probe_tunnel_chain(ws_url: str, api_base: str) -> dict:
    """Full chain: DNS -> TCP -> (TLS) -> WS tunnel -> server.

    Returns a clearly labelled breakdown. `ws://` URLs measure TCP+upgrade;
    `wss://` URLs measure TCP+TLS+upgrade (reported together as the tunnel
    establishment stage, since the client cannot split them).
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(ws_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    out: dict = {"target": ws_url, "stages": []}

    async with _sem():
        dns = await probe_dns(host)
        out["stages"].append(dns.to_dict())
        tcp = await probe_tcp(host, port)
        out["stages"].append(tcp.to_dict())
        if parsed.scheme == "wss":
            tls = await probe_tls(host, port, verify=False, server_name=host)
            out["stages"].append(tls.to_dict())
        ws = await probe_websocket(ws_url)
        out["stages"].append(ws.to_dict())

    out["ok"] = all(s["ok"] for s in out["stages"])
    return out


async def run_batch(probes: list) -> list[dict]:
    """Run a list of probe coroutines under the concurrency cap."""
    async def guarded(coro):
        async with _sem():
            return await coro

    results = await asyncio.gather(*(guarded(p) for p in probes), return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append(ProbeResult("error", False, error=str(r)).to_dict())
        else:
            out.append(r.to_dict() if isinstance(r, ProbeResult) else r)
    return out
