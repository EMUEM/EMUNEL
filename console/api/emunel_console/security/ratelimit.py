"""In-memory sliding-window rate limiter (per IP + bucket).

Good enough for a single control-plane process; the class boundary allows
swapping in a Redis backend for multi-node deployments.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

try:
    from fastapi import HTTPException
except ImportError:  # pragma: no cover
    HTTPException = None


@dataclass
class Rule:
    name: str
    limit: int
    window_seconds: float


RULES = {
    "auth": Rule("auth", limit=30, window_seconds=60),
    "api_write": Rule("api_write", limit=240, window_seconds=60),
    "api_read": Rule("api_read", limit=1200, window_seconds=60),
    "public": Rule("public", limit=300, window_seconds=60),
}


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, rule: Rule) -> None:
        now = time.monotonic()
        with self._lock:
            window = self._hits[key]
            while window and window[0] <= now - rule.window_seconds:
                window.popleft()
            if len(window) >= rule.limit:
                retry_after = int(rule.window_seconds - (now - window[0])) + 1
                if HTTPException is not None:
                    raise HTTPException(
                        status_code=429,
                        detail="rate limit exceeded",
                        headers={"Retry-After": str(retry_after)},
                    )
                raise RuntimeError("rate limit exceeded")
            window.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitASGI:
    """Pure-ASGI rate limiting for the control plane.

    Applied ONLY to /auth/* (mutating endpoints) and /api/*. The public
    gateway (/i/*) is deliberately untouched: proxy clients legitimately
    generate thousands of requests per minute (xHTTP packet-up) and Iranian
    mobile users sit behind carrier-grade NAT, so per-IP limits there would
    cut legitimate traffic.

    Pure ASGI (no BaseHTTPMiddleware) so streaming responses — the xHTTP
    stream-up proxy path — keep flowing byte-by-byte without buffering.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        rule = None
        prefix = ""
        method = (scope.get("method") or "GET").upper()
        if path.startswith("/auth/") and method not in ("GET", "HEAD", "OPTIONS"):
            rule, prefix = RULES["auth"], "auth"
        elif path.startswith("/api/"):
            if method in ("GET", "HEAD", "OPTIONS"):
                rule, prefix = RULES["api_read"], "api"
            else:
                rule, prefix = RULES["api_write"], "api"
        if rule is not None:
            try:
                limiter.check(f"{prefix}:{_scope_ip(scope)}", rule)
            except HTTPException as exc:
                headers = getattr(exc, "headers", None) or {}
                retry_after = headers.get("Retry-After", "2")
                body = json.dumps({"detail": getattr(exc, "detail", "rate limit exceeded")}).encode()
                out = [(b"content-type", b"application/json"),
                       (b"content-length", str(len(body)).encode("latin-1")),
                       (b"retry-after", str(retry_after).encode("latin-1"))]
                await send({"type": "http.response.start", "status": 429, "headers": out})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _scope_ip(scope) -> str:
    for key, value in scope.get("headers", []):
        if (key.decode("latin-1") if isinstance(key, (bytes, bytearray)) else str(key)).lower() == "x-forwarded-for":
            fwd = (value or b"").decode("latin-1")
            if fwd:
                return fwd.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"
