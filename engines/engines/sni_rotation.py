"""SNI Rotation Engine.

Rotates the connect host/SNI of generated configs across a list of
domains the operator attaches to the deployment (EMUNEL_SNI_DOMAINS), so
repeated subscription refreshes spread clients over several SNIs and no
single hostname builds a recognizable traffic pattern.

This is REAL on Railway when the operator attaches multiple custom domains
to the service (every listed domain must resolve to the deployment) and on
self-hosted wildcard-DNS setups. With a single platform domain there is
nothing to rotate between — the engine then reports why it is inactive
instead of breaking configs.

Rotation policy: deterministic round-robin per feed generation (stable,
reproducible, no randomness to debug), keyed by a persisted counter.
"""
from __future__ import annotations

from ..base import KIND_CONFIGGEN, Engine, EngineContext
from .. import configgen_util as cg


class SNIRotationEngine(Engine):
    NAME = "SNIRotation"
    TITLE = "SNI Rotation — spread clients across attached domains"
    HANDLES = frozenset({KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        self.domains = [d for d in self.cfg.sni_domains if d]
        payload = self.state.get(self.NAME, {})
        self.counter = int(payload.get("counter", 0) or 0)
        self.status.metrics.update({"feeds_rotated": 0, "domains": len(self.domains),
                                    "last_domain": ""})

    def preconditions(self) -> str | None:
        if len(self.domains) < 2:
            return ("needs >= 2 domains in EMUNEL_SNI_DOMAINS, all attached to the "
                    "deployment (Railway: Settings -> Domains, one custom domain "
                    "per entry)")
        return None

    async def process(self, ctx: EngineContext) -> EngineContext:
        fmt = ctx.meta.get("format")
        body = ctx.meta.get("body")
        if not isinstance(body, str) or not body or not self.domains:
            return ctx
        domain = self.domains[self.counter % len(self.domains)]
        self.counter += 1
        self.state.mutate(self.NAME, lambda p: p.update({"counter": self.counter}))
        self.status.metrics["feeds_rotated"] += 1
        self.status.metrics["last_domain"] = domain

        if fmt == "singbox":
            payload = cg.singbox_load(body)
            if payload is None:
                return ctx

            def rewrite(outbound: dict) -> None:
                if "server" in outbound:
                    outbound["server"] = domain
                    outbound["server_port"] = outbound.get("server_port") or 443
                tls = outbound.get("tls")
                if isinstance(tls, dict):
                    tls["server_name"] = domain
                transport = outbound.get("transport")
                if isinstance(transport, dict):
                    headers = transport.setdefault("headers", {})
                    if "Host" in headers or "host" in headers:
                        headers["Host"] = domain
                        headers.pop("host", None)

            cg.singbox_transform(payload, rewrite)
            ctx.meta["body"] = cg.singbox_dump(payload)
            return ctx

        if fmt == "clash":
            parsed = cg.clash_parse_proxies(body)
            if parsed is None:
                return ctx
            proxies, _body = parsed
            for proxy in proxies.values():
                proxy["server"] = domain
                if "servername" in proxy:
                    proxy["servername"] = domain
                if "sni" in proxy:
                    proxy["sni"] = domain
                ws = proxy.get("ws-opts")
                if isinstance(ws, dict) and isinstance(ws.get("headers"), dict):
                    ws["headers"]["Host"] = domain
            ctx.meta["body"] = cg.clash_rebuild(body, proxies)
            return ctx

        if fmt == "raw":
            urls = cg.raw_decode(body)
            if urls is None:
                return ctx
            urls = [cg.rewrite_url(u, host=domain, sni=domain, ws_host=domain)
                    for u in urls]
            ctx.meta["body"] = cg.raw_encode(urls)
            return ctx
        return ctx

    def defaults(self) -> dict:
        return {
            "EMUNEL_SNI_DOMAINS": ",".join(self.domains) or "(unset)",
            "rotation": f"round-robin, counter={self.counter}",
        }
