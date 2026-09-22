"""Real HTTP response compression — the Compress engine's working channel.

Why this exists (honest engineering note): on this platform every relayed
proxy byte travels through stock VLESS/Trojan/SS/VMess cores which CANNOT
decompress — so the frame-pipeline compressor could never run on user
traffic without breaking clients. But the panel's own HTTP surface (the
SPA page, its JS/CSS assets, API JSON, subscription feeds) IS plain HTTP
to a browser or client core that sends Accept-Encoding — the one hop
where compression is standard and beneficial.

This middleware gives the Compress engine a REAL, measurable job:

  * gzip via stdlib zlib (always available); brotli additionally when the
    wheel is importable (it already is on this image)
  * streaming-safe: responses are compressed chunk-by-chunk (chunked
    transfer) — subscription feeds and streamed bodies never buffer
  * skips what must be skipped: non-compressible content types (binary
    proxy streams!), responses already encoded, partial content,
    no-transform, SSE (text/event-stream), tiny bodies (<256 bytes)
  * stats (responses, bytes in/out, saved) are surfaced through the
    Compress engine card so the operator sees it working

Activation (either one):
  * env EMUNEL_HTTP_COMPRESSION=true (explicit)
  * the Compress engine hot-enabled on this console (Engine Settings —
    the choice persists across restarts like every toggle)

The data path (websocket relays, /i/* instance proxying) is untouched —
content-type filtering plus a hard path skip for /i/ guarantee it.
"""
from __future__ import annotations

import os
import threading
import zlib

# STABILIZATION (spec 3.2): only bodies worth compressing — 256B default
# (unchanged behaviour); operators running the merged Payload module set
# EMUNEL_HTTP_COMPRESS_MIN_BYTES=1024 for the spec's >1KB rule
MIN_SIZE = max(0, int(os.environ.get("EMUNEL_HTTP_COMPRESS_MIN_BYTES", "256")))

COMPRESSIBLE = (
    "text/", "application/json", "application/javascript",
    "application/xml", "image/svg+xml", "application/x-javascript",
    "application/wasm", "font/ttf",
)
NEVER = ("text/event-stream", "multipart/", "video/", "audio/")

_lock = threading.Lock()
_stats = {"responses": 0, "bytes_in": 0, "bytes_out": 0,
          "skipped": 0, "by_type": {}}


def stats() -> dict:
    with _lock:
        saved = max(0, _stats["bytes_in"] - _stats["bytes_out"])
        return {
            "http_responses": _stats["responses"],
            "http_bytes_in": _stats["bytes_in"],
            "http_bytes_out": _stats["bytes_out"],
            "http_bytes_saved": saved,
            "http_skipped": _stats["skipped"],
            "http_ratio_pct": (round(100.0 * saved / _stats["bytes_in"], 1)
                               if _stats["bytes_in"] else 0.0),
        }


def reset_stats() -> None:
    with _lock:
        for key in ("responses", "bytes_in", "bytes_out", "skipped"):
            _stats[key] = 0
        _stats["by_type"] = {}


def _brotli_available() -> bool:
    try:
        import brotli  # type: ignore

        compressor = getattr(brotli, "Compressor", None)
        return bool(compressor) and any(hasattr(compressor, m)
                                         for m in ("compress", "process"))
    except ImportError:
        return False


class _BrotliStream:
    """Adapter over both brotli flavours: the classic C brotli module
    (Compressor.compress/flush) and brotlicffi-style shims
    (Compressor.process/flush/finish)."""

    def __init__(self, quality: int = 4):
        import brotli

        self._inner = brotli.Compressor(quality=quality)

    def compress(self, data: bytes) -> bytes:
        if hasattr(self._inner, "compress"):
            return self._inner.compress(data)
        return self._inner.process(data) or b""

    def flush(self) -> bytes:
        out = self._inner.flush() or b""
        finish = getattr(self._inner, "finish", None)
        if callable(finish):
            try:
                out += finish() or b""
            except Exception:
                pass
        return out


def _header(scope: dict, name: str) -> str:
    for key, value in scope.get("headers", []):
        if (key.decode("latin-1") if isinstance(key, (bytes, bytearray))
                else str(key)).lower() == name:
            return (value or b"").decode("latin-1", "replace")
    return ""


def _find(headers: list, name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return (value or b"").decode("latin-1", "replace")
    return ""


class HTTPCompressMiddleware:
    """Pure ASGI response compressor; activation checked per request."""

    def __init__(self, app, manager=None, cfg=None):
        self.app = app
        self.manager = manager
        self.env_on = None            # resolved once from the env snapshot
        if cfg is not None:
            self.env_on = bool(getattr(cfg, "http_compress_on", False))

    # ---- activation -------------------------------------------------------
    def _active(self) -> bool:
        if self.env_on:
            return True
        manager = self.manager
        if manager is None:
            return False
        try:
            engine = manager.engines.get("Compress")
            return bool(engine is not None and engine.status.active)
        except Exception:
            return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._active():
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(("/i/", "/worker/")) or scope.get("method") == "HEAD":
            await self.app(scope, receive, send)
            return
        accept = _header(scope, "accept-encoding").lower()
        use_brotli = "br" in accept and _brotli_available()
        use_gzip = "gzip" in accept or "deflate" in accept
        if not (use_brotli or use_gzip):
            await self.app(scope, receive, send)
            return
        await _compressing_call(self.app, scope, receive, send,
                                brotli=use_brotli)


async def _compressing_call(app, scope, receive, send, *, brotli: bool):
    """Run the inner app, compressing its response on the fly."""
    state = {"start_sent": False, "skip": False, "in": 0, "out": 0,
             "ctype": "", "headers": [], "status": 200}
    compressor = None

    def _make_compressor():
        if brotli:
            try:
                return _BrotliStream(quality=4)
            except Exception:
                pass                        # fall through to gzip on any surprise
        return zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

    async def wrapped_send(message):
        nonlocal compressor
        mtype = message.get("type")
        if mtype == "http.response.start":
            state["status"] = message.get("status", 200)
            state["headers"] = list(message.get("headers", []))
            ctype = _find(state["headers"], b"content-type").lower()
            state["ctype"] = ctype
            skip = (state["status"] in (204, 304)
                    or _find(state["headers"], b"content-encoding")
                    or _find(state["headers"], b"content-range")
                    or "no-transform" in _find(state["headers"], b"cache-control").lower()
                    or any(n in ctype for n in NEVER)
                    or not any(c in ctype for c in COMPRESSIBLE))
            if skip:
                state["skip"] = True
                state["start_sent"] = True
                await send(message)          # original headers, untouched
            return
        if mtype != "http.response.body":
            await send(message)
            return
        body = message.get("body", b"") or b""
        more = bool(message.get("more_body", False))
        if state["skip"]:
            await send(message)
            if not more:
                _record(state)
            return
        if compressor is None:
            if not more and len(body) < MIN_SIZE:
                # tiny single-shot response: not worth the header overhead
                state["skip"] = True
                state["start_sent"] = True
                await send({"type": "http.response.start",
                            "status": state["status"],
                            "headers": state["headers"]})
                await send({"type": "http.response.body", "body": body})
                _record(state)
                return
            compressor = _make_compressor()
            headers = [(k, v) for k, v in state["headers"]
                       if k.lower() not in (b"content-length", b"content-encoding")]
            headers.append((b"content-encoding", b"br" if brotli else b"gzip"))
            headers.append((b"vary", b"Accept-Encoding"))
            state["headers"] = headers
            await send({"type": "http.response.start",
                        "status": state["status"], "headers": headers})
            state["start_sent"] = True
        state["in"] += len(body)
        if body:
            chunk = compressor.compress(body)
            state["out"] += len(chunk)
            if chunk:
                await send({"type": "http.response.body", "body": chunk,
                            "more_body": True})
        if not more:
            tail = compressor.flush()
            state["out"] += len(tail)
            await send({"type": "http.response.body", "body": tail,
                        "more_body": False})
            _record(state)

    await app(scope, receive, wrapped_send)


def _record(state: dict) -> None:
    with _lock:
        if state["skip"]:
            _stats["skipped"] += 1
            return
        _stats["responses"] += 1
        _stats["bytes_in"] += state["in"]
        _stats["bytes_out"] += state["out"]
        key = state["ctype"].split(";")[0].strip()[:24] or "unknown"
        _stats["by_type"][key] = _stats["by_type"].get(key, 0) + 1
