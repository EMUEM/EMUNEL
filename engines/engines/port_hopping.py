"""Port Hopping Engine.

Rotates the connect port across EMUNEL_PORT_HOPPING_PORTS in generated
configs so port-based throttling/classification has no stable target.

Deployment reality, stated honestly: Railway exposes ONE public port (443)
per service — a second port in a client config simply cannot reach the
app. The engine is therefore inactive until the operator exposes multiple
ports (self-hosted edge, or a provider that maps several public ports to
the service). With exactly one reachable port there is nothing to hop
between and the engine says so instead of emitting dead configs.

When active, the rotation is deterministic round-robin per feed
generation (persisted counter) — every subscription refresh moves clients
to the next port in the list.
"""
from __future__ import annotations

from ..base import KIND_CONFIGGEN, Engine, EngineContext
from .. import configgen_util as cg


class PortHoppingEngine(Engine):
    NAME = "PortHopping"
    TITLE = "Port Hopping — rotate connect ports across exposed ports"
    HANDLES = frozenset({KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        self.ports = [p for p in self.cfg.porthop_ports if 1 <= p <= 65535]
        payload = self.state.get(self.NAME, {})
        self.counter = int(payload.get("counter", 0) or 0)
        self.status.metrics.update({"feeds_rotated": 0, "ports": len(self.ports),
                                    "last_port": 0})

    def preconditions(self) -> str | None:
        if len(self.ports) < 2:
            return ("needs >= 2 reachable ports in EMUNEL_PORT_HOPPING_PORTS — "
                    "Railway exposes a single public port, so this engine is for "
                    "self-hosted / multi-port edges")
        return None

    async def process(self, ctx: EngineContext) -> EngineContext:
        fmt = ctx.meta.get("format")
        body = ctx.meta.get("body")
        if not isinstance(body, str) or not body or len(self.ports) < 2:
            return ctx
        port = self.ports[self.counter % len(self.ports)]
        self.counter += 1
        self.state.mutate(self.NAME, lambda p: p.update({"counter": self.counter}))
        self.status.metrics["feeds_rotated"] += 1
        self.status.metrics["last_port"] = port

        if fmt == "singbox":
            payload = cg.singbox_load(body)
            if payload is None:
                return ctx

            def rewrite(outbound: dict) -> None:
                if "server" in outbound:
                    outbound["server_port"] = port

            cg.singbox_transform(payload, rewrite)
            ctx.meta["body"] = cg.singbox_dump(payload)
            return ctx

        if fmt == "clash":
            parsed = cg.clash_parse_proxies(body)
            if parsed is None:
                return ctx
            proxies, _body = parsed
            for proxy in proxies.values():
                if "server" in proxy:
                    proxy["port"] = port
            ctx.meta["body"] = cg.clash_rebuild(body, proxies)
            return ctx

        if fmt == "raw":
            urls = cg.raw_decode(body)
            if urls is None:
                return ctx
            urls = [cg.rewrite_url(u, port=port) for u in urls]
            ctx.meta["body"] = cg.raw_encode(urls)
            return ctx
        return ctx

    def defaults(self) -> dict:
        return {
            "EMUNEL_PORT_HOPPING_PORTS": ",".join(str(p) for p in self.ports) or "(none)",
            "rotation": f"round-robin, counter={self.counter}",
        }
