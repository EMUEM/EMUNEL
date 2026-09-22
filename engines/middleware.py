"""ASGI attachment layer — how engines touch traffic WITHOUT touching files.

The console app (and its gateway) stay byte-for-byte identical on disk.
This middleware is installed by main.py AROUND the console app:

  * websocket scopes under /i/*  -> the client-facing relay hop. Downlink
    frames are batched (size/timeout from env), the frame pipeline runs on
    each batch, then frames are written to the client. Uplink frames run
    through the pipeline as single-frame batches (no added latency). WS
    message boundaries are soft for every EMUNEL transport (VLESS/Trojan/
    SS/VMess over WS and xHTTP bodies are byte streams), and the pipeline
    guarantees the concatenated byte stream is preserved.
  * http scopes                  -> two FINITE rewrites only, never the
    streaming instance proxy:
      - /i/<token>/sub responses: the configgen pipeline (split tunneling
        rules, SNI rotation, domain fronting, port rotation) rewrites
        singbox/clash/base64 payloads in place.
      - SPA-fallback responses for non-browser requests: the fake handshake
        engine swaps the panel page for an nginx/apache-style page
        (active-probing defense).

Any engine error -> that batch passes through unchanged (circuit breaker in
the manager). Engines disabled entirely -> this middleware is never even
installed (main.py checks), so behaviour is bit-for-bit the old one.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from .base import KIND_CONFIGGEN, KIND_FRAMES, EngineContext
from .config import EngineEnv

WS_DATA = "websocket.send"
SUB_PREFIX = "/i/"

# live count of gateway WebSocket connections flowing through this layer
# (task 5: feeds the 30s memory-monitor log line; a reconnect storm is
# visible as ws_conns climbing while nothing is actually relayed)
_ws_active = 0


def ws_active_count() -> int:
    return _ws_active


class EnginesASGIMiddleware:
    """Pure ASGI middleware; app on the inside, engines on the outside."""

    def __init__(self, app, manager, cfg: EngineEnv, *, panel_page: str | None = None):
        self.app = app
        self.manager = manager
        self.cfg = cfg
        self.panel_page = panel_page or ""

    async def __call__(self, scope, receive, send):
        global _ws_active
        if scope["type"] == "websocket":
            counted = scope.get("path", "").startswith(SUB_PREFIX)
            if counted:
                _ws_active += 1
            try:
                if counted \
                        and self.manager.pipelines.get(KIND_FRAMES):
                    await self._websocket(scope, receive, send)
                    return
                await self.app(scope, receive, send)
                return
            finally:
                if counted:
                    _ws_active -= 1
        if scope["type"] == "http":
            if scope.get("path", "").startswith(SUB_PREFIX):
                await self._http_gateway(scope, receive, send)
                return
            await self._http_other(scope, receive, send)
            return
        await self.app(scope, receive, send)

    # ------------------------------------------------------------------ websocket
    async def _websocket(self, scope, receive, send):
        conn = _WSConnection(self.manager, self.cfg, send, scope)
        try:
            await self.app(scope, conn.wrap_receive(receive), conn.wrap_send())
        finally:
            await conn.close()

    # ------------------------------------------------------------------ http
    async def _http_gateway(self, scope, receive, send):
        """Finite rewrites for the subscription feed only. Everything else
        (streaming instance proxy!) passes through untouched."""
        path = scope.get("path", "")
        if not (path.endswith("/sub") or path.endswith("/sub/")):
            await self.app(scope, receive, send)
            return
        collector = _SubCollector(self.manager, self.cfg, send, scope)
        await self.app(scope, receive, collector.wrap_send())

    async def _http_other(self, scope, receive, send):
        """Non-gateway HTTP. The fake-handshake engine intercepts SPA
        fallback responses served to non-browser clients (probes)."""
        ua = _header(scope, "user-agent").lower()
        accept = _header(scope, "accept").lower()
        path = scope.get("path", "")
        skip = path.startswith(("/api", "/auth", "/i/", "/worker", "/assets", "/docs")) \
            or path in ("/", "/health", "/ready", "/version",
                        "/favicon.ico", "/favicon.svg", "/favicon.png")
        looks_browser = "mozilla" in ua and "text/html" in accept
        if skip or looks_browser or not self.manager.pipelines.get(KIND_CONFIGGEN):
            await self.app(scope, receive, send)
            return
        interceptor = _FakePageInterceptor(self.manager, self.cfg, send, self.panel_page)
        await self.app(scope, receive, interceptor.wrap_send())


def _header(scope, name: str) -> str:
    for key, value in scope.get("headers", []):
        k = key.decode("latin-1") if isinstance(key, (bytes, bytearray)) else str(key)
        if k.lower() == name:
            return (value or b"").decode("latin-1", errors="replace")
    return ""


class _WSConnection:
    """Per-connection state: downlink batching, pipeline, feedback accounting."""

    def __init__(self, manager, cfg: EngineEnv, send, scope):
        self.manager = manager
        self.cfg = cfg
        self.real_send = send
        self.conn_id = f"gw-{time.monotonic_ns():x}"
        self.client_ip = (scope.get("client") or ("", 0))[0] or "unknown"
        self.meta = {
            "path": scope.get("path", ""),
            "ip": self.client_ip,
            "ua": _header(scope, "user-agent"),
            "conn_id": self.conn_id,
        }
        self.buffer: list[tuple[bytes, bool]] = []   # (data, was_text)
        self.pending = 0
        self._first_ts: float | None = None
        self._flushing = False
        self._closed = False
        self._data_event = asyncio.Event()
        self._flush_task: asyncio.Task | None = None
        self.up_bytes = 0
        self.down_bytes = 0
        self.opened = time.monotonic()

    # ---- send side (downlink: core -> client) --------------------------------
    def wrap_send(self):
        async def send(message):
            mtype = message.get("type")
            if mtype == WS_DATA and (message.get("bytes") is not None
                                     or message.get("text") is not None):
                data = message.get("bytes")
                is_text = data is None
                if is_text:
                    data = (message.get("text") or "").encode("utf-8")
                await self._add(bytes(data), is_text)
                return
            if mtype == "websocket.close":
                await self.flush(final=True)
                self._closed = True
                await self._stop_flusher()
                await self.real_send(message)
                return
            await self.real_send(message)
        return send

    async def _add(self, data: bytes, is_text: bool) -> None:
        if self._closed or not data:
            return
        hard_cap = self.cfg.coalesce_max_buffer
        # backpressure: never let the buffer run away on a stalled client.
        # BOUNDED wait (task 6): a flush stuck longer than the timeout means
        # the client is gone — we drop the connection instead of pinning
        # the coroutine (and its buffers) forever.
        deadline = time.monotonic() + self.cfg.backpressure_timeout_s
        while self.pending >= hard_cap and self._flushing and not self._closed:
            if time.monotonic() >= deadline:
                self._closed = True
                raise ConnectionError("ws backpressure timeout (stalled client)")
            self._data_event.clear()
            try:
                await asyncio.wait_for(
                    self._data_event.wait(),
                    timeout=min(1.0, max(0.01, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                pass
        if self._closed:
            return
        self.buffer.append((data, is_text))
        self.pending += len(data)
        if self._first_ts is None:
            self._first_ts = time.monotonic()
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flusher())
        self._data_event.set()

    async def _flusher(self) -> None:
        """Wakes on data or the coalescing deadline; flushes the batch."""
        try:
            while not self._closed:
                if self.pending == 0:
                    self._data_event.clear()
                    try:
                        await asyncio.wait_for(self._data_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                deadline = (self._first_ts or time.monotonic()) + self.cfg.coalesce_timeout_s
                now = time.monotonic()
                if now < deadline and self.pending < self.cfg.coalesce_max_size:
                    self._data_event.clear()
                    try:
                        await asyncio.wait_for(self._data_event.wait(), timeout=deadline - now)
                    except asyncio.TimeoutError:
                        pass
                if self._closed:
                    return
                if self.pending >= self.cfg.coalesce_max_size \
                        or time.monotonic() >= deadline:
                    await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            # a dead flusher must never kill the relay: flush raw
            with contextlib.suppress(Exception):
                await self.flush()

    async def flush(self, final: bool = False) -> None:
        if self._flushing:
            if final:
                self._closed = True
            return
        if not self.buffer:
            if final:
                self._closed = True
            return
        self._flushing = True
        batch, self.buffer = self.buffer, []
        size, self.pending = self.pending, 0
        self._first_ts = None
        try:
            frames = [data for data, _ in batch]
            ctx = EngineContext(kind=KIND_FRAMES, hop="client", direction="down",
                                conn_id=self.conn_id, frames=frames,
                                meta={**self.meta, "batch_bytes": size})
            try:
                ctx = await self.manager.run_pipeline(ctx)
            except Exception:
                ctx.frames = frames
            out_frames = [bytes(f) for f in (ctx.frames or []) if isinstance(f, (bytes, bytearray)) and f]
            if not out_frames:
                out_frames = frames
            # exact single-frame passthrough keeps text frames as text
            if len(batch) == 1 and len(out_frames) == 1 and out_frames[0] == batch[0][0] \
                    and batch[0][1]:
                await self.real_send({"type": WS_DATA,
                                      "text": out_frames[0].decode("utf-8", "replace")})
                self.down_bytes += len(out_frames[0])
                return
            delay = max(0.0, float(ctx.meta.get("inter_delay_s", 0) or 0))
            sent = 0
            for i, frame in enumerate(out_frames):
                if delay and i > 0:
                    await asyncio.sleep(delay)
                await self.real_send({"type": WS_DATA, "bytes": frame})
                sent += len(frame)
            self.down_bytes += sent
        except Exception:
            # real_send failed (client gone): drop the batch silently
            pass
        finally:
            self._flushing = False
            self._data_event.set()
            if final:
                self._closed = True

    async def _stop_flusher(self) -> None:
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ---- receive side (uplink: client -> core) --------------------------------
    def wrap_receive(self, real_receive):
        async def receive():
            message = await real_receive()
            mtype = message.get("type")
            if mtype == "websocket.receive":
                data = message.get("bytes")
                was_text = data is None
                if was_text:
                    data = (message.get("text") or "").encode("utf-8")
                if data:
                    self.up_bytes += len(data)
                    ctx = EngineContext(kind=KIND_FRAMES, hop="client", direction="up",
                                        conn_id=self.conn_id, frames=[bytes(data)],
                                        meta={**self.meta})
                    try:
                        ctx = await self.manager.run_pipeline(ctx)
                        out = [bytes(f) for f in (ctx.frames or [])
                               if isinstance(f, (bytes, bytearray)) and f]
                        if out and b"".join(out) != data:
                            data = b"".join(out)
                    except Exception:
                        pass
                    if was_text:
                        message = {"type": "websocket.receive",
                                   "text": data.decode("utf-8", "replace")}
                    else:
                        message = {"type": "websocket.receive", "bytes": data}
            elif mtype == "websocket.disconnect":
                with contextlib.suppress(Exception):
                    await self.flush()
            return message
        return receive

    # ---- teardown + feedback ------------------------------------------------------
    async def close(self) -> None:
        await self.flush(final=True)
        await self._stop_flusher()
        # a connection that relayed ZERO frames in both directions is storm
        # churn (rejected/never-relayed), not a Morph sample — sending it
        # would only burn CPU and pollute the bandit with noise per
        # reconnect attempt.
        if self.up_bytes == 0 and self.down_bytes == 0:
            return
        duration = time.monotonic() - self.opened
        payload = {
            "engine": "Morph",
            "conn_id": self.conn_id,
            "ip": self.client_ip,
            "duration": round(duration, 3),
            "up_bytes": self.up_bytes,
            "down_bytes": self.down_bytes,
            "ok": (self.down_bytes >= self.cfg.morph_success_bytes)
                  or (duration >= 10.0 and self.down_bytes > 0),
        }
        with contextlib.suppress(Exception):
            await self.manager.dispatch_feedback(payload)


class _SubCollector:
    """Buffers ONLY the subscription feed response (always finite) so the
    configgen pipeline can rewrite it. Size-capped from env."""

    def __init__(self, manager, cfg: EngineEnv, send, scope):
        self.manager = manager
        self.cfg = cfg
        self.real_send = send
        self.path = scope.get("path", "")
        self.query = (scope.get("query") or b"").decode("latin-1")
        self.host = _header(scope, "host")
        self.cap = cfg.max_http_buffer_bytes
        self.status = 200
        self.headers: list[tuple[bytes, bytes]] = []
        self.chunks: list[bytes] = []
        self.total = 0

    def wrap_send(self):
        async def send(message):
            mtype = message.get("type")
            if mtype == "http.response.start":
                self.status = message.get("status", 200)
                self.headers = list(message.get("headers", []))
                return
            if mtype == "http.response.body":
                body = message.get("body", b"") or b""
                if not message.get("more_body", False):
                    await self._finish(body)
                    return
                self.chunks.append(body)
                self.total += len(body)
                if self.total > self.cap:
                    await self._emit_raw()
                return
            await self.real_send(message)
        return send

    async def _emit_raw(self) -> None:
        """Too big to rewrite safely: emit what we hold, untouched."""
        if self.status is not None:
            await self.real_send({"type": "http.response.start",
                                  "status": self.status, "headers": self.headers})
            self.status = None
        for chunk in self.chunks:
            await self.real_send({"type": "http.response.body", "body": chunk,
                                  "more_body": True})
        self.chunks, self.total = [], 0

    async def _finish(self, tail: bytes) -> None:
        body = b"".join(self.chunks) + tail
        self.chunks, self.total = [], 0
        content_type = _find_header(self.headers, b"content-type")
        fmt = None
        if "application/json" in content_type:
            fmt = "singbox"
        elif "yaml" in content_type:
            fmt = "clash"
        elif "text/plain" in content_type:
            fmt = "raw"
        if fmt is None or self.status != 200:
            await self.real_send({"type": "http.response.start",
                                  "status": self.status, "headers": self.headers})
            await self.real_send({"type": "http.response.body", "body": body})
            return
        ctx = EngineContext(
            kind=KIND_CONFIGGEN, hop="client", conn_id="sub",
            frames=[],
            meta={"format": fmt, "body": body.decode("utf-8", errors="replace"),
                  "headers": [(k.decode("latin-1"), v.decode("latin-1"))
                              for k, v in self.headers],
                  "path": self.path, "query": self.query, "host": self.host},
        )
        try:
            ctx = await self.manager.run_pipeline(ctx)
        except Exception:
            pass
        out = ctx.meta.get("body")
        payload = out.encode("utf-8") if isinstance(out, str) else body
        headers = [(k, v) for k, v in self.headers if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(payload)).encode("latin-1")))
        await self.real_send({"type": "http.response.start",
                              "status": self.status, "headers": headers})
        await self.real_send({"type": "http.response.body", "body": payload})


class _FakePageInterceptor:
    """Swaps SPA-fallback responses (panel HTML) for a web-server-style page
    when the requester does not look like a browser — active-probing defense.

    The browser experience is untouched: browser-like requests still get the
    panel. Everything else sees a plain nginx/apache default page."""

    def __init__(self, manager, cfg: EngineEnv, send, panel_page: str):
        self.manager = manager
        self.cfg = cfg
        self.real_send = send
        self.panel_page = panel_page or "\x00"   # never-match sentinel
        self.status = 200
        self.headers: list[tuple[bytes, bytes]] = []
        self.chunks: list[bytes] = []

    def wrap_send(self):
        async def send(message):
            mtype = message.get("type")
            if mtype == "http.response.start":
                self.status = message.get("status", 200)
                self.headers = list(message.get("headers", []))
                return
            if mtype == "http.response.body":
                if message.get("more_body", False):
                    self.chunks.append(message.get("body", b"") or b"")
                    return
                full = b"".join(self.chunks) + (message.get("body", b"") or b"")
                self.chunks = []
                try:
                    if full.decode("utf-8", errors="replace").strip() == self.panel_page.strip():
                        await self._emit_fake()
                        return
                except Exception:
                    pass
                await self.real_send({"type": "http.response.start",
                                      "status": self.status, "headers": self.headers})
                await self.real_send({"type": "http.response.body", "body": full})
                return
            await self.real_send(message)
        return send

    async def _emit_fake(self) -> None:
        from .engines.fake_handshake import fake_page, fake_headers

        page = fake_page(self.cfg)
        status, extra = fake_headers(self.cfg)
        headers = [(k, v) for k, v in self.headers if k.lower() not in
                   (b"content-type", b"content-length", b"server")]
        headers.extend(extra)
        await self.real_send({"type": "http.response.start", "status": status,
                              "headers": headers})
        await self.real_send({"type": "http.response.body", "body": page})


def _find_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return (value or b"").decode("latin-1", errors="replace").lower()
    return ""
