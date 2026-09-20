"""Shared helpers for the configgen engines (subscription feed rewrites).

The middleware hands each configgen engine the subscription body already
decoded (ctx.meta["body"]) plus its format ("singbox" JSON / "clash" YAML
lines / "raw" base64 v2ray URL list). The helpers here parse and re-emit
each format; engines transform in between. Every rewriter is defensive:
when parsing fails the body passes through untouched (probes and clients
never see a broken feed).
"""
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

# ---- raw (base64 v2ray list) ---------------------------------------------------

def raw_decode(body: str) -> list[str] | None:
    try:
        text = base64.b64decode(body.strip() + "=" * (-len(body.strip()) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return [line for line in text.splitlines() if line.strip()]


def raw_encode(urls: list[str]) -> str:
    return base64.b64encode("\n".join(urls).encode("utf-8")).decode("ascii")


def rewrite_url(url: str, *, host: str | None = None, sni: str | None = None,
                ws_host: str | None = None, port: int | None = None) -> str:
    """Rewrite connection parameters of a vless/trojan/ss URL; vmess is
    rewritten via its base64 JSON. Anything else returns unchanged."""
    scheme = urlsplit(url).scheme.lower()
    if scheme == "vmess":
        return _rewrite_vmess(url, host=host, sni=sni, ws_host=ws_host, port=port)
    if scheme not in ("vless", "trojan", "ss"):
        return url
    try:
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        hostname = parts.hostname or ""
        netloc = parts.netloc
        if host:
            qhost = host
            netloc = netloc.replace(hostname, host, 1) if hostname in netloc else netloc
            if "@" not in netloc and host not in netloc:
                # userinfo-less netloc is just host:port
                netloc = host + (f":{parts.port}" if parts.port else "")
        if port:
            # rebuild netloc with the new port
            userinfo = netloc.rsplit("@", 1)[0] + "@" if "@" in netloc else ""
            hp = netloc.rsplit("@", 1)[-1].rsplit(":", 1)[0]
            netloc = f"{userinfo}{hp}:{port}"
        if sni:
            q["sni"] = sni
        if ws_host:
            q["host"] = ws_host
        query = urlencode({k: v for k, v in q.items()}, quote_via=quote)
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except ValueError:
        return url


def _rewrite_vmess(url: str, *, host=None, sni=None, ws_host=None, port=None) -> str:
    try:
        payload = url[8:]
        data = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
        if host:
            data["add"] = host
        if port:
            data["port"] = str(port)
        if sni:
            data["sni"] = sni
        if ws_host:
            data["host"] = ws_host
        return "vmess://" + base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode()).decode()
    except (ValueError, KeyError):
        return url


# ---- sing-box JSON ---------------------------------------------------------------

def singbox_load(body: str) -> dict | None:
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def singbox_dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def singbox_transform(payload: dict, fn) -> dict:
    for outbound in payload.get("outbounds", []):
        if isinstance(outbound, dict):
            fn(outbound)
    return payload


# ---- clash YAML (our own emitter's shape) -------------------------------------------
# The gateway emits proxies as one inline JSON object per "  - " line, so we
# can parse them as JSON without a YAML dependency. Unknown shapes pass through.

def clash_parse_proxies(body: str) -> tuple[dict[int, dict], str] | None:
    """Returns ({line_index: proxy}, body) or None when unparseable."""
    proxies: dict[int, dict] = {}
    in_proxies = False
    lines = body.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "proxies:":
            in_proxies = True
            continue
        if in_proxies:
            if stripped.startswith("- {") and stripped.endswith("}"):
                try:
                    proxies[i] = json.loads(stripped[2:])
                except ValueError:
                    return None
            elif not stripped.startswith("- "):
                in_proxies = False
    return proxies, body


def clash_rebuild(body: str, proxies: dict[int, dict]) -> str:
    lines = body.splitlines()
    for i, proxy in proxies.items():
        lines[i] = "  - " + json.dumps(proxy, ensure_ascii=False)
    return "\n".join(lines) + ("\n" if body.endswith("\n") else "")


def clash_insert_rules(body: str, rules: list[str]) -> str:
    """Insert DIRECT rules before the final MATCH rule."""
    if "rules:" not in body:
        return body
    lines = body.splitlines()
    out: list[str] = []
    inserted = False
    for line in lines:
        if not inserted and line.strip().startswith("- MATCH"):
            for rule in rules:
                out.append("  - " + rule)
            inserted = True
        out.append(line)
    if not inserted:  # no MATCH line: append rules at the end
        for rule in rules:
            out.append("  - " + rule)
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")
