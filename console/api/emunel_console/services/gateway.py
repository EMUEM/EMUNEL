"""Instance endpoint gateway.

On platforms that expose a single public HTTP endpoint per deployment,
every EMUNEL instance is reached through the Console's public URL under a private
endpoint token:

    https://<console-host>/i/<endpoint-token>/<core-path>

The gateway authenticates by endpoint token (un guessable, rotatable),
strips the prefix, and proxies HTTP and WebSocket traffic to the instance's
EMUNEL Core through the Worker. On self-hosted deployments with wildcard DNS
the same instances can additionally be exposed as real hostnames via the
bundled Caddy — both modes share this proxy path.

WebSocket proxying is implemented frame-by-frame (client ⇄ gateway ⇄ core)
because the standard HTTP client stack cannot pass an Upgrade through.
"""
from __future__ import annotations

import asyncio
import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from ..db import get_pool
from ..logging import get
from .subscription import render_subscription as _sub_html_page

log = get("network", "emunel.console.gateway")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "authorization",  # replaced with the worker token below
}


FRIENDLY_404 = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>EMUNEL</title>
<style>body{{background:#0a0c10;color:#e7ebf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.c{{max-width:420px;text-align:center;padding:28px;border:1px solid #1e2430;border-radius:12px;background:#12151c}}
h2{{margin:0 0 8px}}p{{color:#9aa4b8;font-size:13.5px;line-height:1.55}}
code{{background:#10131a;border:1px solid #2a3242;border-radius:6px;padding:1px 6px;font-size:12px}}</style></head>
<body><div class="c"><h2>{title}</h2><p>{body}</p></div></body></html>"""


def _page(title: str, body: str, status: int = 200) -> "HTMLResponse":
    from fastapi.responses import HTMLResponse

    return HTMLResponse(FRIENDLY_404.format(title=title, body=body), status_code=status)


# ── storm hardening (reconnect-loop / OOM fix) ──────────────────────────────
# Task 1/2/3 of the gateway stabilization: proxy clients that get a 404
# (or a bare close) reconnect IMMEDIATELY with no backoff. Three levers:
#   * stopped instances answer 503 + Retry-After ("come back later")
#   * non-browser clients get a ~30-byte body instead of the friendly
#     HTML page (thousands of storm hits stopped paying for kilobytes)
#   * WebSocket relays are capped (1013 = try-again-later), their tasks
#     are awaited on teardown, and upstream sockets carry write timeouts
import os as _os_env

_WS_MAX_CONNECTIONS = max(0, int(_os_env.environ.get("EMUNEL_WS_MAX_CONNECTIONS", "60")))
_WS_MAX_FRAME_BYTES = int(_os_env.environ.get("EMUNEL_WS_MAX_FRAME_BYTES", str(4 * 1024 * 1024)))
_WS_WRITE_TIMEOUT = float(_os_env.environ.get("EMUNEL_WS_WRITE_TIMEOUT", "10"))


def _is_browser_request(request) -> bool:
    """Browsers always send a Mozilla UA *and* Accept: text/html; proxy
    clients never do (same heuristic as the subscription handler)."""
    ua = (request.headers.get("user-agent") or "").lower()
    accept = (request.headers.get("accept") or "").lower()
    return "mozilla" in ua and "text/html" in accept


def _tiny(status: int, text: str, retry_after: int | None = None):
    """Minimal plain-text reply for non-browser (proxy-client) traffic."""
    from fastapi.responses import Response as _R

    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return _R(content=f"emunel: {text}\n", status_code=status,
              media_type="text/plain", headers=headers)


class _WsGuard:
    """Cap + counter for live gateway WebSocket relays (task 3).

    Server-side truth for monitoring; the 30s memory monitor line reads
    `.active` so the reconnect storm is visible in the Railway logs."""

    def __init__(self, cap: int):
        self.cap = cap
        self.active = 0
        self.rejected = 0

    def acquire(self) -> bool:
        if 0 < self.cap <= self.active:
            self.rejected += 1
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


_ws_guard = _WsGuard(_WS_MAX_CONNECTIONS)


async def _bounded_send(aw) -> None:
    """Every relay send carries a timeout (websockets 16 has no
    write_timeout kwarg): a peer that stops reading is cut when its
    send buffer stalls, instead of pinning the relay task forever."""
    await asyncio.wait_for(aw, timeout=_WS_WRITE_TIMEOUT)


def ws_active_count() -> int:
    """Live WS relay count for the engines memory-monitor line (task 5)."""
    return _ws_guard.active


def _stream_upstream(upstream_resp):
    """aiter_raw wrapped so the upstream response is ALWAYS aclose()'d.

    Starlette skips BackgroundTask cleanup when the client disconnects
    mid-stream (the response coroutine is cancelled); a leaked response
    keeps its pool connection checked out forever and the shared client
    eventually deadlocks (max_connections reached, pool timeout=None).
    The generator's finally runs on both the normal and the cancelled
    path (asyncgen shutdown hooks), which releases the connection."""
    async def _gen():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            try:
                await upstream_resp.aclose()
            except Exception:
                pass
    return _gen()


def _singbox_outbound(url: str) -> dict:
    """vless:// / trojan:// URI -> sing-box outbound. Shadowsocks links pass
    through parsed minimally; unsupported schemes are skipped by caller."""
    import base64 as _b64u
    from urllib.parse import urlparse, parse_qs, unquote

    u = urlparse(url)
    if u.scheme == "vmess":
        import json
        data = json.loads(_b64u.b64decode(url[8:] + "=" * (-len(url[8:]) % 4)))
        return {"type": "vmess", "tag": data.get("ps") or "emunel",
                "server": data["add"], "server_port": int(data["port"]),
                "uuid": data["id"], "security": data.get("scy", "auto"), "alter_id": 0,
                "tls": {"enabled": data.get("tls") == "tls", "server_name": data.get("sni") or data["add"], "alpn": ["http/1.1"]},
                "transport": {"type": "ws", "path": data["path"], "headers": {"Host": data.get("host") or data["add"]}}}
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("type") == "xhttp":
        raise HTTPException(422, detail="xHTTP cannot be represented by this sing-box exporter; use raw subscription with an xHTTP-compatible Xray client")
    tag = unquote(u.fragment) or "emunel"
    common = {"tag": tag}
    if u.scheme in ("vless", "trojan"):
        inner = {
            "server": u.hostname or "",
            "server_port": u.port or 443,
            "uuid": u.username or "" if u.scheme == "vless" else None,
            "password": u.username or "" if u.scheme == "trojan" else None,
            "tls": {
                "enabled": q.get("security") == "tls",
                "server_name": q.get("sni") or u.hostname or "",
                "utls": {"enabled": True, "fingerprint": q.get("fp", "chrome")} if q.get("fp") else None,
            },
            "transport": {
                "type": "ws",
                "path": q.get("path", "/"),
                "headers": {"Host": q.get("host") or u.hostname or ""},
            } if q.get("type") == "ws" else None,
        }
        common["type"] = u.scheme
        out = {k: v for k, v in inner.items() if v is not None}
        tls = out.get("tls") or {}
        if tls.get("utls") is None:
            tls.pop("utls", None)
        if out.get("transport") is None:
            out.pop("transport", None)
        out.update(common)
        return out
    if u.scheme == "ss":
        userinfo = u.username or ""
        pad = "=" * (-len(userinfo) % 4)
        try:
            method, password = _b64u.b64decode(userinfo + pad).decode().split(":", 1)
        except Exception:
            method, password = "aes-256-gcm", ""
        return {"type": "shadowsocks", "tag": tag, "server": u.hostname or "",
                "server_port": u.port or 443, "method": method, "password": password}
    return {"type": u.scheme, "tag": tag}


def _clash_proxy(url: str) -> dict | None:
    """vless/trojan URI -> Clash Meta proxy map (vless needs Meta)."""
    from urllib.parse import urlparse, parse_qs, unquote

    u = urlparse(url)
    if u.scheme == "vmess":
        import base64
        import json
        data = json.loads(base64.b64decode(url[8:] + "=" * (-len(url[8:]) % 4)))
        return {"name": data.get("ps") or "emunel", "type": "vmess", "server": data["add"],
                "port": int(data["port"]), "uuid": data["id"], "alterId": 0,
                "cipher": data.get("scy", "auto"), "tls": data.get("tls") == "tls",
                "servername": data.get("sni") or data["add"], "alpn": ["http/1.1"],
                "network": "ws", "ws-opts": {"path": data["path"], "headers": {"Host": data.get("host") or data["add"]}}}
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("type") == "xhttp":
        raise HTTPException(422, detail="xHTTP cannot be represented by this Clash exporter; use raw subscription with an xHTTP-compatible Xray client")
    name = unquote(u.fragment) or "emunel"
    if u.scheme == "vless":
        return {"name": name, "type": "vless", "server": u.hostname or "",
                "port": u.port or 443, "uuid": u.username or "",
                "udp": True, "tls": q.get("security") == "tls",
                "servername": q.get("sni") or u.hostname or "",
                "client-fingerprint": q.get("fp", "chrome"),
                "network": "ws", "ws-opts": {"path": q.get("path", "/"),
                "headers": {"Host": q.get("host") or u.hostname or ""}}}
    if u.scheme == "trojan":
        return {"name": name, "type": "trojan", "server": u.hostname or "",
                "port": u.port or 443, "password": u.username or "",
                "udp": True, "sni": q.get("sni") or u.hostname or "",
                "client-fingerprint": q.get("fp", "chrome"),
                "network": "ws", "ws-opts": {"path": q.get("path", "/"),
                "headers": {"Host": q.get("host") or u.hostname or ""}}}
    if u.scheme == "ss":
        import base64 as _b64u

        pad = "=" * (-len(u.username or "") % 4)
        try:
            method, password = _b64u.b64decode((u.username or "") + pad).decode().split(":", 1)
        except Exception:
            return None
        return {"name": name, "type": "ss", "server": u.hostname or "",
                "port": u.port or 443, "cipher": method, "password": password}
    return None


def _clash_quote(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def _clash_inline(p: dict) -> str:
    import json as _json

    return _json.dumps(p, ensure_ascii=False)


router = APIRouter(include_in_schema=False)


# ── endpoint resolution cache ────────────────────────────────────────────────
# Every proxied request (proxy clients can generate thousands per minute on
# xHTTP transports) used to run TWO DB queries and 2-3 INFO log lines. The
# TTL cache removes both costs: positive results live EMUNEL_ENDPOINT_CACHE_SECONDS
# (15s), negative results 4s so dead endpoints stop hammering the database
# during client reconnect storms (the Railway "5.6K requests / 50% errors" spike).
#
# The cache is keyed per event loop: one entry-set per running loop. In
# production there is exactly one loop for the process lifetime; per-loop
# keying merely keeps test runs (one loop per test) from reading each other's
# resolutions.
import asyncio as _asyncio
import os as _os
import time as _time
import weakref as _weakref

_CACHE_TTL = max(2.0, float(_os.environ.get("EMUNEL_ENDPOINT_CACHE_SECONDS", "15")))
_CACHE_TTL_NEG = min(_CACHE_TTL, max(1.0, float(_os.environ.get("EMUNEL_ENDPOINT_CACHE_NEG_SECONDS", "4"))))
_CACHE_MAX = 2048
_caches_by_loop: "weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()


def _loop_cache() -> dict[str, tuple[float, dict | None]]:
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return _caches_by_loop.setdefault(_fallback_loop_key, {})
    return _caches_by_loop.setdefault(loop, {})


class _FallbackLoopKey:
    """Sentinel loop key for calls outside any event loop (sync code)."""


_fallback_loop_key = _FallbackLoopKey()


def _cache_get(token: str) -> dict | None | bool:
    """Cached target (dict), cached miss (None) or False when not cached."""
    entry = _loop_cache().get(token)
    if entry is None:
        return False
    expires, value = entry
    if _time.time() >= expires:
        _loop_cache().pop(token, None)
        return False
    return value


def _cache_put(token: str, value: dict | None) -> None:
    cache = _loop_cache()
    if len(cache) >= _CACHE_MAX:
        # cheap opportunistic trim: drop the ~25% oldest entries
        for key in list(sorted(cache, key=lambda k: cache[k][0]))[: _CACHE_MAX // 4]:
            cache.pop(key, None)
    ttl = _CACHE_TTL if value is not None else _CACHE_TTL_NEG
    cache[token] = (_time.time() + ttl, value)


def invalidate_endpoint_cache(instance_id: str | None = None) -> None:
    """Drop cached resolutions (all, or one instance's) after lifecycle changes."""
    for cache in list(_caches_by_loop.values()) + [_caches_by_loop.get(_fallback_loop_key, {})]:
        if instance_id is None:
            cache.clear()
            continue
        for key in [k for k, (_, v) in cache.items()
                    if isinstance(v, dict) and v.get("instance_id") == instance_id]:
            cache.pop(key, None)


async def _resolve_endpoint(request: Request, token: str) -> dict | None:
    """endpoint token -> {instance_id, worker_url, status} (TTL-cached)."""
    cached = _cache_get(token)
    if cached is not False:
        return cached
    pool = get_pool(request)
    from ..config import settings as _s
    from ..security.token_codec import decode_token

    instance_id = decode_token(token, _s.secret_key)
    if instance_id is not None:
        row = await pool.fetchrow(
            """
            SELECT i.id, i.status,
                   (SELECT d.domain FROM domains d WHERE d.instance_id = i.id
                     AND d.is_active = TRUE AND d.kind = 'path'
                     ORDER BY d.created_at DESC LIMIT 1) AS endpoint_token,
                   (SELECT dep.node_id FROM deployments dep WHERE dep.instance_id = i.id
                     ORDER BY dep.started_at DESC LIMIT 1) AS node_id
            FROM instances i WHERE i.id = $1
            """,
            instance_id,
        )
    else:
        row = await pool.fetchrow(
            """
            SELECT i.id, i.status,
                   (SELECT d.domain FROM domains d WHERE d.instance_id = i.id
                     AND d.is_active = TRUE AND d.kind = 'path'
                     ORDER BY d.created_at DESC LIMIT 1) AS endpoint_token,
                   (SELECT dep.node_id FROM deployments dep WHERE dep.instance_id = i.id
                     ORDER BY dep.started_at DESC LIMIT 1) AS node_id
            FROM instances i
            WHERE i.id IN (SELECT instance_id FROM domains
                            WHERE kind = 'path' AND domain = $1 AND is_active = TRUE)
            """,
            token,
        )
    if row is not None and row["endpoint_token"] is None:
        row = None
    if row is None:
        log.debug("endpoint resolve miss (token[:10]=%s)", token[:10])
        _cache_put(token, None)
        return None
    if row["status"] != "running":
        # KNOWN instance that is not running (restarting / crashed / OOM):
        # a TEMPORARY state, not a dead endpoint. Cached on the short
        # negative TTL like a miss, but carried so callers can answer
        # 503 + Retry-After instead of a 404 — a 404 tells xHTTP clients
        # the endpoint is gone forever and they reconnect in a tight
        # loop (the Railway log storm).
        _cache_put(token, {"stopped": True})
        return {"stopped": True}
    from ..services.workers import worker_url_for

    target = {
        "instance_id": str(row["id"]),
        "worker_url": worker_url_for(row["node_id"] or "local"),
        "upstream": f"/worker/api/instances/{row['id']}/proxy",
    }
    _cache_put(token, target)
    return target


# ── shared loopback client ───────────────────────────────────────────────────
# A fresh AsyncClient PER proxied request churned connection pools and TLS
# contexts and showed up as the container's memory climbing under load; one
# bounded shared client serves every gateway + subscription hop instead.
# Keyed by event loop: exactly one client in production (single loop), and
# test runs (one loop per test) never reuse a client bound to a dead loop.
_clients_by_loop: "weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()


def _worker_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients_by_loop.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        _clients_by_loop[loop] = client
    return client


@router.get("/i/{token}")
async def instance_status_page(token: str, request: Request):
    """Browser-friendly view of a proxy endpoint (the path itself is for
    proxy clients, not people)."""
    target = await _resolve_endpoint(request, token)
    if target is None:
        return _page(
            "Endpoint not found",
            "This endpoint doesn't exist or its instance was removed. "
            "If you recently redeployed EMUNEL without a persistent volume, "
            "create a new instance in the panel and copy its fresh config "
            "from the <b>Config</b> tab.",
            status=404,
        )
    if target.get("stopped"):
        return _page(
            "Instance restarting",
            "The instance behind this endpoint is starting or restarting. "
            "Proxy clients should retry shortly — this is temporary, the "
            "endpoint is still valid.",
            status=503,
        )
    return _page(
        "This endpoint is live",
        "This address is the private transport path for your proxy client — "
        "there is no web page here. Open the EMUNEL panel, choose your "
        "instance, open the <b>Config</b> tab and copy the "
        "<code>vless://</code> link into your client (v2rayNG, NekoBox, "
        "Streisand, …).",
    )


@router.post("/i/{token}/api/qr")
async def instance_qr_public(token: str, request: Request):
    """QR (SVG) for the subscription page — authorized by the endpoint token."""
    import io

    import qrcode
    import qrcode.image.svg
    from fastapi.responses import Response as _Response

    target = await _resolve_endpoint(request, token)
    if target is None or target.get("stopped"):
        raise HTTPException(status_code=404 if target is None else 503,
                             detail="unknown endpoint" if target is None
                             else "instance not running")
    body = await request.json()
    text = str(body.get("text") or "")[:4096]
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return _Response(content=buf.getvalue(), media_type="image/svg+xml")


@router.get("/i/{token}/sub")
async def instance_subscription(token: str, request: Request):
    """Subscription: ALL protocols of this instance. Auth = endpoint token.
    Formats via ?fmt=: singbox | clash | (default) base64 v2ray list.
    Content host: ?host=, panel-announced host, or request host."""
    import base64 as _b64

    import httpx as _httpx

    from ..config import settings as _settings

    fmt = (request.query_params.get("fmt") or "").strip().lower()

    target = await _resolve_endpoint(request, token)
    if target is None:
        return _page(
            "Endpoint not found",
            "This subscription doesn't exist or its instance was removed. "
            "Create a new instance in the EMUNEL panel and copy its "
            "subscription URL from the Config tab.",
            status=404,
        )
    pool = get_pool(request)
    inst = await pool.fetchrow(
        "SELECT name, public_host, status FROM instances WHERE id = $1", target["instance_id"]
    )
    if inst is None or inst["status"] != "running":
        return _page("Instance not running",
                     "The subscription will work once the instance is running.", status=503)
    host = (request.query_params.get("host")
            or inst["public_host"]
            or (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
            or request.headers.get("host") or "").split(":")[0]
    if not host:
        return _page("Missing host", "Append ?host=<your-domain> to this URL.", status=400)
    try:
        client = _worker_client()
        resp = await client.post(
            f"{target['worker_url'].rstrip('/')}{target['upstream']}/core/api/share",
            json={"host": host, "path_prefix": f"/i/{token}", "uuids": []},
            headers={"Authorization": f"Bearer {_settings.worker_token}",
                     "Content-Type": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        configs = [c for c in resp.json().get("links", []) if c.get("share_url")]
        links = [c["share_url"] for c in configs]
    except Exception as exc:
        return _page("Unavailable", f"Could not read the instance configs: {str(exc)[:160]}",
                     status=502)

    # Real quota state — the same numbers the panel shows. Two levels:
    # 1. the instance envelope (volume cap / time limit), and
    # 2. when no envelope is set, the aggregate of the per-config quotas
    #    (AHB group-subscription semantics: sum of caps, earliest expiry).
    # Feeds both the client-parsed header and the HTML page; unlimited only
    # when nothing anywhere sets a limit.
    from . import volume as _volume_svc

    try:
        quota = await _volume_svc.get_state(pool, target["instance_id"])
    except Exception:
        quota = None
    if quota is not None and not quota.get("limit_bytes") and not quota.get("expires_at"):
        # No instance envelope — fall back to the per-config aggregate.
        try:
            from . import links as _links_svc

            aggregate = await _links_svc.aggregate_quota(
                pool, target["instance_id"], quota.get("used_bytes") or 0
            )
        except Exception:
            aggregate = None
        if aggregate is not None:
            quota = aggregate

    # Per-config real state for the HTML page (usage meters per config card).
    link_states: dict = {}
    try:
        from . import links as _links_svc

        states = await _links_svc.list_links(pool, target["instance_id"], live=True)
        link_states = {l["uuid"]: l for l in states.get("links", [])}
    except Exception:
        link_states = {}

    title = f"EMUNEL \u00b7 {inst['name']}"
    from fastapi.responses import Response as _Response

    # ── Browser detection: HTML page for people, raw payload for clients ──
    # DEFAULT IS RAW: proxy clients (v2rayN, v2rayNG, Happ, …) may send
    # browser-like or empty User-Agents, so UA guessing alone would feed them
    # the HTML page and break imports. HTML is served only when the request
    # looks like a real web browser: Mozilla-style UA *and* "Accept:
    # text/html" (browsers always send both; proxy clients never do).
    # Explicit ?fmt= always forces raw data.
    ua = (request.headers.get("user-agent") or "").lower()
    accept = (request.headers.get("accept") or "").lower()
    looks_like_browser = "mozilla" in ua and "text/html" in accept

    if looks_like_browser and not fmt:
        from fastapi.responses import HTMLResponse

        return HTMLResponse(_sub_html_page(title, configs, host, f"/i/{token}/sub",
                                           qr_path=f"/i/{token}/api/qr", quota=quota,
                                           link_states=link_states))

    # The quota the client sees is the instance's REAL state: total = the
    # volume cap, expire = the time limit, download = current usage. 0 (or
    # absence) keeps the previous Default — unlimited — behavior.
    userinfo = _volume_svc.userinfo_header(quota)

    def _headers(extra: dict | None = None) -> dict:
        h = {
            "profile-title": "base64:" + _b64.b64encode(title.encode()).decode(),
            "subscription-userinfo": userinfo,
            "profile-update-interval": "24",
            "profile-web-page-url": f"{request.url.scheme}://{request.headers.get('host', host)}",
        }
        if _settings.telegram_channel:
            h["support-url"] = _settings.telegram_channel
        if extra:
            h.update(extra)
        return h

    # Format negotiation:
    #   ?fmt=singbox  -> sing-box JSON (Outbounds)
    #   ?fmt=clash    -> Clash YAML (proxies)
    #   default       -> base64 v2ray list (v2rayNG, NekoBox, Streisand, ArasClient)
    if fmt in ("singbox", "sing-box", "sb"):
        import json as _json

        outbounds = [_singbox_outbound(u) for u in links]
        payload = _json.dumps({"outbounds": outbounds}, ensure_ascii=False, indent=2)
        return _Response(content=payload, media_type="application/json",
                         headers=_headers())

    if fmt in ("clash", "clash-meta", "yaml"):
        proxies = [_clash_proxy(u) for u in links]
        proxies = [p for p in proxies if p]
        names = [p["name"] for p in proxies]
        payload = (
            "port: 7890\nsocks-port: 7891\nallow-lan: false\nmode: rule\nlog-level: warning\n"
            "proxies:\n"
            + "\n".join("  - " + _clash_inline(p) for p in proxies)
            + "\nproxy-groups:\n  - name: EMUNEL\n    type: select\n    proxies:\n"
            + "".join(f"      - {_clash_quote(n)}\n" for n in names)
            + "rules:\n  - MATCH,EMUNEL\n"
        )
        return _Response(content=payload, media_type="text/yaml", headers=_headers())

    from .subscription import TELEGRAM_CONFIG

    if TELEGRAM_CONFIG:
        links = [TELEGRAM_CONFIG] + [url for url in links if url != TELEGRAM_CONFIG]
    body = _b64.b64encode("\n".join(links).encode()).decode()
    return _Response(content=body, media_type="text/plain", headers=_headers())


@router.api_route("/i/{token}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def instance_http_gateway(token: str, path: str, request: Request):
    target = await _resolve_endpoint(request, token)
    if target is None:
        # Unknown/deleted endpoint. Browsers get the friendly page; proxy
        # clients get ~30 bytes — a 404-storm must not pay for kilobytes.
        if _is_browser_request(request):
            return _page(
                "Endpoint not found",
                "This endpoint doesn't exist or its instance is not running. "
                "Check the panel — if the instance is Running, copy the fresh "
                "config from its <b>Config</b> tab.",
                status=404,
            )
        return _tiny(404, "endpoint not found")
    if target.get("stopped"):
        # Instance EXISTS but is not running (restart / crash / OOM):
        # 503 + Retry-After is the correct signal — 404 made xHTTP clients
        # treat the endpoint as permanently gone and reconnect in a
        # tight loop (the endless stream-up 404 spam in the logs).
        if _is_browser_request(request):
            return _page(
                "Instance restarting",
                "The instance behind this endpoint is starting or restarting. "
                "Proxy clients should retry shortly.",
                status=503,
            )
        return _tiny(503, "instance not running", retry_after=5)

    worker_url = target["worker_url"].rstrip("/")
    url = f"{worker_url}{target['upstream']}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
    headers.append(("X-EMUNEL-Endpoint", token))
    from ..config import settings as _cfg

    headers.append(("Authorization", f"Bearer {_cfg.worker_token}"))

    client = _worker_client()
    try:
        # Stream request bodies too: xHTTP stream-up POSTs are infinite upload
        # streams — buffering via request.body() would wait forever and the
        # first chunk would never reach Core (stream-up configs never connect).
        content = None if request.method in ("GET", "HEAD", "OPTIONS") else request.stream()
        upstream_req = client.build_request(
            request.method, url, headers=headers, params=None, content=content,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
        return StreamingResponse(
            # wrapped stream: the response is closed even when the client
            # disconnects mid-stream (BackgroundTask alone does NOT run on
            # that path — the leaked connection would deadlock the pool)
            _stream_upstream(upstream_resp),
            status_code=upstream_resp.status_code,
            headers={k: v for k, v in upstream_resp.headers.items()
                     if k.lower() not in HOP_BY_HOP},
            background=_release_response(upstream_resp),
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="instance upstream unavailable")


def _release_response(resp):
    """Shared client: only the response is closed after streaming — the
    client itself (and its bounded connection pool) stays alive for the next
    hop instead of being rebuilt per request."""
    from starlette.background import BackgroundTask

    async def _cleanup() -> None:
        await resp.aclose()

    return BackgroundTask(_cleanup)


@router.websocket("/i/{token}/{path:path}")
async def instance_ws_gateway(ws: WebSocket, token: str, path: str):
    """Frame-level WebSocket relay into the instance's EMUNEL Core.

    Accept the client up front (so failures produce proper close codes, not
    Starlette's HTTP 403 rejection), then open the upstream through the
    worker's ws-proxy and pump frames in both directions.

    Storm hardening: connections are capped (task 3 — over-cap clients get
    1013 try-again-later), a stopped instance closes with 1013 (retry) while
    a dead endpoint stays 1008 (do not retry), the relay tasks are always
    awaited on teardown so nothing is left half-open, and the upstream
    socket carries frame/write bounds (task 6) so a stalled hop cannot
    pin buffers or tasks forever.
    """
    await ws.accept()
    if not _ws_guard.acquire():
        log.warning("ws gateway: connection cap reached (%d active) — rejecting",
                    _ws_guard.active)
        try:
            await ws.close(code=1013, reason="try again later")
        except Exception:
            pass
        return
    try:
        try:
            target = await _resolve_endpoint(ws, token)
        except Exception as _exc:
            import traceback as _tb

            log.error("WS resolve failed: %s | %s", _exc, _tb.format_exc()[-400:])
            target = None
        if target is None:
            await ws.close(code=1008, reason="unknown or inactive instance endpoint")
            return
        if target.get("stopped"):
            # instance exists but is restarting — "try again", not "go away"
            await ws.close(code=1013, reason="instance restarting")
            return

        # Build the upstream ws URL through the worker's websocket proxy
        # (separate route from the HTTP /proxy path).
        worker_ws = target["worker_url"].replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
        from ..config import settings as _settings

        upstream_url = (
            f"{worker_ws}/worker/api/instances/{target['instance_id']}/ws-proxy/{path}"
            f"?token={_settings.worker_token}"
        )

        # Forward selected client headers so Core sees the real client IP etc.
        client_headers = {}
        for key in ("x-forwarded-for", "x-real-ip", "user-agent"):
            val = ws.headers.get(key)
            if val:
                client_headers[key] = val
        client_headers["x-emunel-endpoint"] = token

        try:
            async with websockets.connect(
                upstream_url,
                additional_headers=client_headers,  # type: ignore[arg-type]
                max_size=_WS_MAX_FRAME_BYTES,   # was None — unbounded frame buffer
                max_queue=32,                    # bounded inbound frame queue
                write_limit=1024 * 1024,          # bufferedAmount cap (task 6)
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as upstream:
                async def client_to_upstream() -> None:
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                return
                            data = msg.get("bytes")
                            if data is not None:
                                await _bounded_send(upstream.send(data))
                            else:
                                text = msg.get("text")
                                if text is not None:
                                    await _bounded_send(upstream.send(text))
                    except (WebSocketDisconnect, Exception):
                        return

                async def upstream_to_client() -> None:
                    try:
                        async for message in upstream:
                            if isinstance(message, (bytes, bytearray)):
                                await _bounded_send(ws.send_bytes(bytes(message)))
                            else:
                                await _bounded_send(ws.send_text(message))
                    except Exception:
                        return

                done, pending = await asyncio.wait(
                    {asyncio.create_task(client_to_upstream()),
                     asyncio.create_task(upstream_to_client())},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    # actually release those tasks' resources before we
                    # return (cancel() alone left them half-open)
                    await asyncio.gather(*pending, return_exceptions=True)
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            log.info("gateway ws upstream failed: %s", type(exc).__name__)
            await ws.close(code=1014, reason="upstream unavailable")
            return
        finally:
            try:
                await ws.close()
            except Exception:
                pass
    finally:
        _ws_guard.release()
