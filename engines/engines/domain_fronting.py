"""Domain Fronting Engine (configgen option for self-hosted edges).

Operator-facing definition (as specified): the client connects with an
SNI that belongs to a domestic, unfiltered domain (the "front"), while the
HTTP layer carries the real host — an edge server that owns the fronting
domain terminates TLS and routes by Host to the real backend.

Reality on each deployment type, stated honestly:
  * Railway: TLS is terminated at the platform edge and routed BY SNI, so
    fronting with an SNI we do not own cannot reach the app — the engine
    refuses to activate unless it detects a self-hosted edge setup
    (EMUNEL_FRONTING_SNI_DOMAIN + EMUNEL_FRONTING_REAL_HOST both set) and
    marks the requirement in its status.
  * Self-hosted: run the bundled Caddy (deploy/proxy) on a domestic server
    with a certificate for the fronting domain; it proxies to the real
    backend by Host header. The engine then rewrites generated configs:
    connect/SNI -> fronting domain, WS Host -> real host.

As a panel option it appears as a toggle that only takes effect when the
preconditions are present (no silent breakage).
"""
from __future__ import annotations

from ..base import KIND_CONFIGGEN, Engine, EngineContext
from .. import configgen_util as cg


class DomainFrontingEngine(Engine):
    NAME = "DomainFronting"
    TITLE = "Domain Fronting — domestic SNI edge, real host inside"
    HANDLES = frozenset({KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        self.front = self.cfg.fronting_sni_domain.strip()
        self.real = self.cfg.fronting_real_host.strip()
        self.status.metrics.update({"feeds_fronted": 0, "rewrites": 0})

    def preconditions(self) -> str | None:
        if not (self.front and self.real):
            return ("needs EMUNEL_FRONTING_SNI_DOMAIN (domestic front domain) + "
                    "EMUNEL_FRONTING_REAL_HOST (real backend host); requires the "
                    "self-hosted Caddy edge — see engines/README.md")
        return None

    async def process(self, ctx: EngineContext) -> EngineContext:
        fmt = ctx.meta.get("format")
        body = ctx.meta.get("body")
        if not isinstance(body, str) or not body or not (self.front and self.real):
            return ctx
        rewrites = 0

        if fmt == "singbox":
            payload = cg.singbox_load(body)
            if payload is None:
                return ctx

            def rewrite(outbound: dict) -> None:
                nonlocal rewrites
                if "server" not in outbound:
                    return
                outbound["server"] = self.front
                outbound["server_port"] = outbound.get("server_port") or 443
                tls = outbound.setdefault("tls", {"enabled": True})
                tls["enabled"] = True
                tls["server_name"] = self.front
                transport = outbound.setdefault("transport", {})
                headers = transport.setdefault("headers", {})
                headers["Host"] = self.real
                rewrites += 1

            cg.singbox_transform(payload, rewrite)
            ctx.meta["body"] = cg.singbox_dump(payload)

        elif fmt == "clash":
            parsed = cg.clash_parse_proxies(body)
            if parsed is None:
                return ctx
            proxies, _body = parsed
            for proxy in proxies.values():
                proxy["server"] = self.front
                proxy["tls"] = True
                if "servername" in proxy:
                    proxy["servername"] = self.front
                if "sni" in proxy:
                    proxy["sni"] = self.front
                ws = proxy.setdefault("ws-opts", {})
                ws.setdefault("headers", {})["Host"] = self.real
                rewrites += 1
            ctx.meta["body"] = cg.clash_rebuild(body, proxies)

        elif fmt == "raw":
            urls = cg.raw_decode(body)
            if urls is None:
                return ctx
            urls = [cg.rewrite_url(u, host=self.front, sni=self.front, ws_host=self.real)
                    for u in urls]
            rewrites = len(urls)
            ctx.meta["body"] = cg.raw_encode(urls)
        else:
            return ctx

        self.status.metrics["feeds_fronted"] += 1
        self.status.metrics["rewrites"] += rewrites
        return ctx

    def defaults(self) -> dict:
        return {
            "EMUNEL_FRONTING_SNI_DOMAIN": self.front or "(unset)",
            "EMUNEL_FRONTING_REAL_HOST": self.real or "(unset)",
        }
