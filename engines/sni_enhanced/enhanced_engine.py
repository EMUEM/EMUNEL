"""SNI Enhanced engine — stateful-DPI evasion control plane (service engine).

WHAT IT IS (honest scope, same rule as the basic SNISpoof engine): the
Railway panel sits AFTER the censor — spoofing itself executes on the
user's device. This engine is the CONTROL PLANE for the advanced
technique set:

  * profile management (technique, strategy, fooling, seqovl, repeats,
    TTL, fingerprint, tlsrec) with ISP presets (ISP_STRATEGIES.json)
  * the allowed-SNI pool (ir_allowed_snis.json) with adaptive weights
  * byte-level PLAN generation + server-side PROOF for every technique
    (fragment / fake_sni / combined / hostfakesplit / multisplit /
    multidisorder / fakedsplit / fakeddisorder / tlsrec / oob / wrong_seq /
    md5sig / syndata / synack) — plans are proven stream-preserving
  * the CDN/decoy-target scanner (bounded, rate-limited, history-capped)
  * per-technique success metrics (measured server-side on every test)
  * the downloadable enhanced client helper implementing the fallback
    ladder on the user's device:
        enhanced techniques -> basic SNISpoof -> plain direct connect

FALLBACK LADDER (operator spec):
  1. SNI_ENHANCED_ENABLED=false          -> basic SNI Spoofing engine
  2. enhanced module/plan errors         -> basic SNI Spoofing engine
  3. basic engine fails                  -> direct connection
  4. everything fails                    -> Core default (plain passthrough)

Feature flag: SNI_ENHANCED_ENABLED (default false). Zero pipeline kinds —
the relay data path never touches this engine; the Core is not involved.
"""
from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from ..base import Engine
from .injection import (FOOLING, FRAGMENT_STRATEGIES, TECHNIQUES, InjectionPlan,
                        plan_injection, tlsrec_wrap, verify_stream, verify_tlsrec)
from .scanner import DEFAULT_TARGETS, ScanRunner, parse_targets
from .sni_pool import SNIPool, load_default_snis, load_strategies

_PACKAGE_DIR = Path(__file__).resolve().parent
HELPER_FILE = _PACKAGE_DIR / "emunel_sni_enhanced_helper.py"

# Multi-layer fallback ladder (operator spec, verbatim order)
FALLBACK_CHAIN = [
    {"step": "SNIEnhanced", "condition": "SNI_ENHANCED_ENABLED=true and a valid profile"},
    {"step": "SNISpoof (basic)", "condition": "enhanced disabled OR any enhanced-module error"},
    {"step": "direct connection", "condition": "basic engine fails"},
    {"step": "Core default", "condition": "everything above failed"},
]


class SNIEnhancedEngine(Engine):
    NAME = "SNIEnhanced"
    TITLE = ("SNI Enhanced — stateful DPI evasion: advanced techniques, "
             "ISP strategies, scanner (client-side helper)")
    HANDLES = frozenset()            # service engine: API + tab, no pipeline hop
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002 (interface)
        stored = self.state.get(self.NAME, {}) or {}
        self.profile = self._merge_profile(stored.get("profile") or {})
        self.technique_stats: dict = dict(stored.get("technique_stats") or {})
        self.pool = SNIPool(list(self.profile.get("sni_pool") or []))
        if not (self.profile.get("sni_pool") or []):
            self.profile["sni_pool"] = self.pool.list()
        self.scanner = ScanRunner(min_interval=max(
            5.0, float(self.cfg.snienhanced_scan_min_interval)))
        if isinstance(stored.get("scan_history"), list):
            self.scanner.history = [
                h for h in stored["scan_history"] if isinstance(h, dict)
            ][-10:]
        self._scan_task: asyncio.Task | None = None
        self._apply_isp_strategy(self.profile.get("strategy") or "auto", quiet=True)
        self.status.metrics.update({
            "profiles_set": int(stored.get("profiles_set", 0) or 0),
            "tests_run": int(stored.get("tests_run", 0) or 0),
            "helper_downloads": int(stored.get("helper_downloads", 0) or 0),
            "scans_run": int(stored.get("scans_run", 0) or 0),
        })

    async def start(self) -> None:
        self._scan_task = asyncio.create_task(self._scan_loop())
        self.log.info(
            f"profile: technique={self.profile['technique']} "
            f"strategy={self.profile['strategy']} fooling={self.profile['fooling']} "
            f"seqovl={self.profile['seqovl']} pool={len(self.pool)} — "
            "execution runs on client devices via the enhanced helper")

    async def stop(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scan_task = None
        await self.flush_state()

    def preconditions(self) -> str | None:
        if not self.pool.list():
            return ("allowed-SNI pool is empty — add SNIs or restore "
                    "ir_allowed_snis.json (the engine stays a pure generator)")
        return None

    def defaults(self) -> dict:
        return {
            "technique": self.profile["technique"],
            "strategy": self.profile["strategy"],
            "fooling": self.profile["fooling"],
            "seqovl": self.profile["seqovl"],
            "repeats": self.profile["repeats"],
            "fragment_strategy": self.profile["fragment_strategy"],
            "fragment_delay": self.profile["fragment_delay"],
            "ttl_trick": self.profile["ttl_trick"],
            "ttl_value": self.profile["ttl_value"],
            "fingerprint": self.profile["fingerprint"],
            "tlsrec": self.profile["tlsrec"],
            "pool_size": len(self.pool),
        }

    # ---- profile -------------------------------------------------------------
    def _merge_profile(self, override: dict) -> dict:
        p = {
            "technique": self.cfg.snienhanced_method,
            "strategy": "auto",
            "fooling": self.cfg.snienhanced_fooling,
            "seqovl": self.cfg.snienhanced_seqovl,
            "repeats": 1,
            "fragment_strategy": "sni_split",
            "split_pos": 1,
            "midsld": True,
            "fragment_delay": self.cfg.snienhanced_fragment_delay,
            "ttl_trick": True,
            "ttl_value": self.cfg.snienhanced_ttl_value,
            "fingerprint": self.cfg.snienhanced_fingerprint,
            "tlsrec": False,
            "sni_pool": list(self.cfg.snienhanced_pool or []),
            "scan_targets": "",
            "scan_interval_hours": self.cfg.snienhanced_scan_interval_hours,
        }
        if isinstance(override, dict) and override:
            p.update({k: override[k] for k in p if k in override})
        if p["technique"] not in TECHNIQUES:
            p["technique"] = "combined"
        if p["fooling"] not in FOOLING:
            p["fooling"] = "md5sig"
        if p["fragment_strategy"] not in FRAGMENT_STRATEGIES:
            p["fragment_strategy"] = "sni_split"
        return p

    def set_profile(self, body: dict) -> dict:
        """Validate + persist profile keys (only provided keys change)."""
        update: dict = {}
        if "technique" in body or "method" in body:
            technique = str(body.get("technique") or body.get("method") or "").strip().lower()
            if technique not in TECHNIQUES:
                raise ValueError(f"technique must be one of: {', '.join(TECHNIQUES)}")
            update["technique"] = technique
        for key, cast, lo, hi in (
            ("seqovl", int, 0, 65535), ("repeats", int, 1, 8),
            ("split_pos", int, 1, 65535), ("ttl_value", int, 1, 8),
            ("scan_interval_hours", int, 1, 48),
        ):
            if key in body:
                try:
                    value = int(body.get(key))
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be an integer")
                if not lo <= value <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
                update[key] = value
        if "fragment_delay" in body or "delay" in body:
            try:
                delay = float(body.get("fragment_delay", body.get("delay")))
            except (TypeError, ValueError):
                raise ValueError("fragment_delay must be a number (seconds)")
            if not 0.05 <= delay <= 2.0:
                raise ValueError("fragment_delay must be 0.05-2.0s (0.1-0.5 recommended)")
            update["fragment_delay"] = delay
        if "fooling" in body:
            fooling = str(body.get("fooling") or "").strip().lower()
            if fooling not in FOOLING:
                raise ValueError(f"fooling must be one of: {', '.join(FOOLING)}")
            update["fooling"] = fooling
        if "fragment_strategy" in body or "strategy_name" in body:
            strategy = str(body.get("fragment_strategy") or "").strip().lower()
            if strategy not in FRAGMENT_STRATEGIES:
                raise ValueError(f"fragment_strategy must be one of: {', '.join(FRAGMENT_STRATEGIES)}")
            update["fragment_strategy"] = strategy
        for flag in ("midsld", "ttl_trick", "tlsrec"):
            if flag in body:
                update[flag] = bool(body.get(flag))
        if "fingerprint" in body:
            fp = str(body.get("fingerprint") or "").strip().lower()
            if fp not in ("chrome", "firefox", "safari", "randomized"):
                raise ValueError("fingerprint must be chrome|firefox|safari|randomized")
            update["fingerprint"] = fp
        if "sni_pool" in body:
            update["sni_pool"] = self.pool.replace(body.get("sni_pool") or [])
        if "scan_targets" in body:
            raw = body.get("scan_targets") or ""
            if isinstance(raw, list):
                raw = ",".join(str(x) for x in raw)
            targets = parse_targets(str(raw))
            update["scan_targets"] = ",".join(f"{h}:{p}" for h, p in targets)
        if "strategy" in body:
            self._apply_isp_strategy(str(body.get("strategy") or "auto"))
            update.update({k: v for k, v in self.profile.items()
                           if k in ("technique", "fooling", "repeats", "seqovl",
                                    "split_pos", "midsld", "fingerprint", "strategy")})
        self.profile.update(update)
        self.profile["strategy"] = self.profile.get("strategy") or "auto"
        if update.get("sni_pool"):
            self.profile["sni_pool"] = update["sni_pool"]
        stored = self.state.get(self.NAME, {}) or {}
        stored["profile"] = dict(self.profile)
        self.state.set(self.NAME, stored)
        self.status.metrics["profiles_set"] = int(self.status.metrics.get("profiles_set", 0)) + 1
        self.log.info("profile updated: " + (", ".join(sorted(update)) or "strategy"))
        return dict(self.profile)

    def _apply_isp_strategy(self, strategy: str, quiet: bool = False) -> None:
        """Load an ISP preset from ISP_STRATEGIES.json onto the profile."""
        strategies = load_strategies()
        name = (strategy or "auto").strip().lower()
        preset = strategies.get(name)
        if not preset:
            if not quiet:
                raise ValueError(
                    f"unknown strategy {strategy!r} — available: "
                    + ", ".join(sorted(strategies)))
            preset = strategies["auto"]
            name = "auto"
        self.profile.update({
            "strategy": name,
            "technique": preset.get("method") or self.profile["technique"],
            "fooling": preset.get("fooling") or self.profile["fooling"],
            "repeats": int(preset.get("repeats") or 1),
            "seqovl": int(preset.get("seqovl") or 0),
            "split_pos": int(preset.get("split_pos") or 1),
            "midsld": bool(preset.get("midsld")),
            "fingerprint": preset.get("fingerprint") or "chrome",
        })

    # ---- proof / test ----------------------------------------------------------
    def run_test(self, technique: str | None = None) -> dict:
        """Build a real ClientHello, plan the requested technique (or the
        profile's), verify the plan and record per-technique stats.

        Runs entirely server-side: this proves the PLANNER the helper uses.
        """
        from .handshake_builder import build_fake_client_hello, find_sni_span

        rng = random.Random()
        name = (technique or self.profile["technique"]).strip().lower()
        if name not in TECHNIQUES:
            raise ValueError(f"technique must be one of: {', '.join(TECHNIQUES)}")
        real_hello = build_fake_client_hello("emunel-enhanced-test.example.com",
                                             self.profile["fingerprint"], rng)
        span = find_sni_span(real_hello)
        try:
            plan = plan_injection(real_hello, {**self.profile, "technique": name},
                                  self.pool, rng)
        except Exception as exc:
            self._stat(name, ok=False)
            return {"ok": False, "technique": name, "error": f"planner failed: {exc}"}
        data_parts = [s.payload for s in plan.steps if s.kind == "data"]
        stream_ok = (not data_parts) or (
            b"".join(data_parts) == real_hello
            or b"".join(reversed(data_parts)) == real_hello   # fakeddisorder
            or (name == "tlsrec" and verify_tlsrec(real_hello, data_parts)))
        recs = tlsrec_wrap(real_hello)
        tlsrec_ok = verify_tlsrec(real_hello, recs)
        fake_steps = [s for s in plan.steps if s.kind == "fake"]
        ok = bool(span and plan.steps and stream_ok
                  and (not fake_steps or fake_steps[0].payload[:1] == b"\x16"))
        self._stat(name, ok=ok)
        stored = self.state.get(self.NAME, {}) or {}
        stored["tests_run"] = int(stored.get("tests_run", 0) or 0) + 1
        self.state.set(self.NAME, stored)
        self.status.metrics["tests_run"] = int(self.status.metrics.get("tests_run", 0)) + 1
        return {
            "ok": ok,
            "technique": name,
            "parsed_sni": span[2] if span else None,
            "hello_bytes": len(real_hello),
            "steps": [s.as_dict() for s in plan.steps],
            "fake_sni": plan.fake_sni,
            "stream_preserved": stream_ok,
            "raw_socket_required": plan.raw_socket_required,
            "warnings": plan.warnings,
            "tlsrec_proof": tlsrec_ok,
            "fallback_note": ("raw-socket steps are planned as templates; the "
                             "userspace ladder (fragment/hostfakesplit/tlsrec/"
                             "low-TTL decoy) always executes") if plan.raw_socket_required
                            else None,
        }

    def _stat(self, technique: str, *, ok: bool) -> None:
        stat = self.technique_stats.setdefault(technique, {"ok": 0, "fail": 0})
        stat["ok" if ok else "fail"] += 1
        stored = self.state.get(self.NAME, {}) or {}
        stored["technique_stats"] = dict(self.technique_stats)
        self.state.set(self.NAME, stored)

    def technique_success_rates(self) -> dict:
        rates = {}
        for name, stat in self.technique_stats.items():
            total = int(stat.get("ok", 0)) + int(stat.get("fail", 0))
            rates[name] = {
                "ok": int(stat.get("ok", 0)),
                "fail": int(stat.get("fail", 0)),
                "rate": round(100.0 * int(stat.get("ok", 0)) / total, 1) if total else None,
            }
        return rates

    # ---- scanner ---------------------------------------------------------------
    async def scan(self, targets: str | list | None = None, *, force: bool = True) -> dict:
        raw = targets if targets else self.profile.get("scan_targets")
        target_list = parse_targets(raw) if raw else parse_targets(DEFAULT_TARGETS)
        sni = self.pool.pick() if len(self.pool) else ""
        scan = await self.scanner.run(target_list, sni=sni, force=force)
        if self._state_bytes() > self._state_cap_bytes():
            # volume guard: stop persisting scan history, keep serving the
            # cached one (degraded but functional — plans never depend on it)
            self.scanner.history = self.scanner.history[-2:]
            scan["degraded"] = "engine state over its volume cap — history trimmed"
            self.log.warning("state over volume cap — scan history trimmed")
        else:
            stored = self.state.get(self.NAME, {}) or {}
            stored["scans_run"] = int(stored.get("scans_run", 0) or 0) + 1
            stored["scan_history"] = self.scanner.history
            self.state.set(self.NAME, stored)
            self.status.metrics["scans_run"] = int(self.status.metrics.get("scans_run", 0)) + 1
        self.log.info(f"scan: {scan.get('alive', 0)}/{scan.get('probed', 0)} targets alive"
                      + (" (rate-limited — cached)" if scan.get("rate_limited") else ""))
        return scan

    def _state_cap_bytes(self) -> int:
        return max(64, int(self.cfg.snienhanced_max_state_kb)) * 1024

    def _state_bytes(self) -> int:
        try:
            return (Path(self.state.data_dir()) / "state.json").stat().st_size
        except OSError:
            return 0

    async def _scan_loop(self) -> None:
        """Background auto-scan every SNI_SCAN_INTERVAL_HOURS (default 6h)."""
        while True:
            interval = max(1, int(self.profile.get("scan_interval_hours") or 6)) * 3600
            await asyncio.sleep(interval)
            try:
                await self.scan(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(f"auto-scan failed: {exc}")

    # ---- helper + status ---------------------------------------------------------
    def helper_source(self) -> str:
        try:
            source = HELPER_FILE.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - packaging error
            raise RuntimeError(f"helper script missing from the image: {exc}") from exc
        stored = self.state.get(self.NAME, {}) or {}
        stored["helper_downloads"] = int(stored.get("helper_downloads", 0) or 0) + 1
        self.state.set(self.NAME, stored)
        self.status.metrics["helper_downloads"] = \
            int(self.status.metrics.get("helper_downloads", 0)) + 1
        return source

    def helper_usage(self) -> str:
        p = self.profile
        return (f"python emunel_sni_enhanced_helper.py --connect <server-host>:443 "
                f"--technique {p['technique']} --strategy {p['fragment_strategy']} "
                f"--delay {p['fragment_delay']}"
                + (f" --ttl {p['ttl_value']}" if p["ttl_trick"] else "")
                + f" --fooling {p['fooling']} --pool {','.join(self.pool.list()[:4])}")

    def status_payload(self) -> dict:
        cached = self.scanner.cached()
        return {
            "profile": dict(self.profile),
            "pool": self.pool.snapshot(),
            "techniques": list(TECHNIQUES),
            "fooling": list(FOOLING),
            "fragment_strategies": list(FRAGMENT_STRATEGIES),
            "strategies": {name: {"label": p.get("label", name),
                                  "description": p.get("description", "")}
                           for name, p in load_strategies().items()},
            "success_rates": self.technique_success_rates(),
            "metrics": self.snapshot_metrics(),
            "helper_usage": self.helper_usage(),
            "scanner": {
                "last_scan": cached,
                "history_count": len(self.scanner.history),
                "interval_hours": self.profile.get("scan_interval_hours", 6),
                "default_targets": DEFAULT_TARGETS,
                "scan_targets": self.profile.get("scan_targets") or "",
            },
            "fallback_chain": FALLBACK_CHAIN,
            "fallback_now": ("SNIEnhanced" if self.pool.list() else "SNISpoof (basic)"),
        }

    async def flush_state(self) -> None:
        stored = self.state.get(self.NAME, {}) or {}
        stored["profile"] = dict(self.profile)
        stored["technique_stats"] = dict(self.technique_stats)
        if self._state_bytes() > self._state_cap_bytes():
            stored["scan_history"] = self.scanner.history[-1:]
        else:
            stored["scan_history"] = self.scanner.history
        self.state.set(self.NAME, stored)
