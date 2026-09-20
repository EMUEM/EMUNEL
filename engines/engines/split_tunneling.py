"""Iran Split Tunneling Engine.

Injects direct-routing rules for Iranian domains into the generated
sing-box / Clash subscription payloads, so *.ir, banks, Aparat, Snapp and
friends bypass the tunnel entirely:

  * client speed: domestic sites stop paying the round-trip to the server
  * effective volume: Iranian traffic no longer eats the user's quota
  * sanity: bank/gov traffic behaves exactly as without a proxy

Scope, honestly: rule injection works for ?fmt=singbox and ?fmt=clash
feeds (both carry routing tables). The plain base64 v2ray list has no
routing concept — those clients keep using the tunnel for everything, and
this engine reports that in its status rather than pretending.

Domain list from EMUNEL_SPLIT_DOMAINS (bundled default covers .ir plus
the popular non-.ir Iranian services).
"""
from __future__ import annotations

from ..base import KIND_CONFIGGEN, Engine, EngineContext
from .. import configgen_util as cg

DIRECT_TAG = "emunel-direct"


class SplitTunnelingEngine(Engine):
    NAME = "SplitTunnel"
    TITLE = "Iran Split Tunneling — .ir and Iranian services go direct"
    HANDLES = frozenset({KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        self.domains = [d.strip().lstrip(".").lower() for d in self.cfg.split_domains if d.strip()]
        self.status.metrics.update({
            "feeds_rewritten": 0, "rules_injected": 0,
            "raw_feeds_skipped": 0, "domains_covered": len(self.domains),
        })

    def preconditions(self) -> str | None:
        if not self.domains:
            return "EMUNEL_SPLIT_DOMAINS resolved to an empty list"
        return None

    async def process(self, ctx: EngineContext) -> EngineContext:
        fmt = ctx.meta.get("format")
        body = ctx.meta.get("body")
        if not isinstance(body, str) or not body:
            return ctx
        if fmt == "singbox":
            new_body = self._singbox(body)
        elif fmt == "clash":
            new_body = self._clash(body)
        else:
            # raw base64 list: v2ray URL format carries no routing rules —
            # count and be honest about it instead of faking support.
            self.status.metrics["raw_feeds_skipped"] += 1
            return ctx
        if new_body != body:
            ctx.meta["body"] = new_body
            self.status.metrics["feeds_rewritten"] += 1
        return ctx

    # ---- sing-box -----------------------------------------------------------------
    def _singbox(self, body: str) -> str:
        payload = cg.singbox_load(body)
        if payload is None:
            return body
        suffixes = sorted({d for d in self.domains if not d.startswith("geoip")})

        def add_direct(outbound: dict) -> None:
            pass  # outbounds stay as-is; routing lives in route.rules

        cg.singbox_transform(payload, add_direct)
        route = payload.setdefault("route", {})
        rules = route.setdefault("rules", [])
        new_rules = [
            {"domain_suffix": [f".{d}" for d in suffixes], "outbound": DIRECT_TAG},
        ]
        if self.cfg.split_ip_cidrs:
            new_rules.append({"ip_cidr": list(self.cfg.split_ip_cidrs), "outbound": DIRECT_TAG})
        if any(r.get("outbound") == DIRECT_TAG for r in rules if isinstance(r, dict)):
            return cg.singbox_dump(payload)   # idempotent
        # direct outbound must exist for the rules to resolve
        outbounds = payload.setdefault("outbounds", [])
        if not any(o.get("tag") == DIRECT_TAG for o in outbounds if isinstance(o, dict)):
            outbounds.append({"type": "direct", "tag": DIRECT_TAG})
        route["rules"] = new_rules + rules
        self.status.metrics["rules_injected"] += sum(len(r.get("domain_suffix", []))
                                                     for r in new_rules)
        return cg.singbox_dump(payload)

    # ---- clash --------------------------------------------------------------------
    def _clash(self, body: str) -> str:
        if any(line.strip().startswith("- DOMAIN-SUFFIX,") for line in body.splitlines()):
            if "emunel-direct" in body or ",DIRECT" in body:
                return body  # already routed
        rules = [f"DOMAIN-SUFFIX,{d},DIRECT" for d in sorted(self.domains)]
        for keyword in ("irancell", "shatel", "mci"):
            rules.append(f"DOMAIN-KEYWORD,{keyword},DIRECT")
        if self.cfg.split_ip_cidrs:
            for cidr in self.cfg.split_ip_cidrs:
                rules.append(f"IP-CIDR,{cidr},DIRECT")
        self.status.metrics["rules_injected"] += len(rules)
        return cg.clash_insert_rules(body, rules)

    def defaults(self) -> dict:
        return {
            "EMUNEL_SPLIT_DOMAINS": f"{len(self.domains)} domains (first: "
                                    f"{', '.join(self.domains[:6])}...)",
            "EMUNEL_SPLIT_IP_CIDRS": ",".join(self.cfg.split_ip_cidrs) or "(none)",
            "note": "applies to singbox/clash feeds; raw base64 lists carry no routing",
        }
