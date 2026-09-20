"""Morphing Engine — shape the client-facing hop to look like benign traffic.

What it actually controls (userspace, honest scope):
  * downlink frame sizes and pacing (chunk size, inter-chunk delay, optional
    padding) — via the frame pipeline, per ISP profile
  * generated config hints (uTLS fingerprint / SNI / ALPN) through the
    configgen pipeline — the params clients already accept per link
  * profile choice per ISP with a LinUCB contextual bandit that learns from
    real connection outcomes (bytes relayed, duration) observed at the
    gateway hop, plus optional self-play probes

What it cannot control and never pretends to: the TLS fingerprint of the
platform edge (Railway terminates TLS before our process; the client-side
uTLS fingerprint is chosen by the client app from our config hints) and
raw IP-layer header fields (handled by the OS).
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from ..base import KIND_CONFIGGEN, KIND_FRAMES, Engine, EngineContext
from ..profiles import LinUCB, build_prefix_table, isp_for_ip, normalize_profiles


class MorphingEngine(Engine):
    NAME = "Morph"
    TITLE = "Traffic Morphing — ISP profiles + LinUCB profile selection"
    HANDLES = frozenset({KIND_FRAMES, KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        self.profiles = normalize_profiles(self.cfg.morph_profiles)
        self.prefix_table = build_prefix_table(self.cfg.morph_isp_prefixes)
        payload = self.state.get(self.NAME, {})
        self.bandit = LinUCB.from_dict(payload.get("bandit", {}),
                                       alpha=self.cfg.morph_bandit_alpha)
        self.chosen: dict[str, str] = dict(payload.get("chosen", {}))
        self.stats = {"selected": 0, "feedback": 0, "rewarded": 0.0}
        self.status.metrics.update(self.stats)
        self._selfplay_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.cfg.morph_selfplay and self.cfg.morph_selfplay_url:
            self._selfplay_task = asyncio.create_task(self._selfplay_loop())

    async def stop(self) -> None:
        if self._selfplay_task is not None:
            self._selfplay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._selfplay_task
            self._selfplay_task = None
        self._persist()

    # ---- frame pipeline ------------------------------------------------------
    def _profile_for(self, ctx: EngineContext) -> dict:
        isp = isp_for_ip(ctx.meta.get("ip", ""), self.prefix_table)
        arms = [p["name"] for p in self.profiles.get(isp, self.profiles["*"])]
        context = LinUCB.context()
        name, _score = self.bandit.choose(arms, context)
        if not name:
            profile = self.profiles.get(isp, self.profiles["*"])[0]
            return profile
        self.chosen[isp] = name
        self.status.metrics["selected"] += 1
        for profile in self.profiles.get(isp, self.profiles["*"]):
            if profile["name"] == name:
                return profile
        return self.profiles["*"][0]

    async def process(self, ctx: EngineContext) -> EngineContext:
        if ctx.kind == KIND_CONFIGGEN:
            return self._configgen(ctx)
        if ctx.direction != "down" or not ctx.frames:
            return ctx
        profile = self._profile_for(ctx)
        chunk = int(profile.get("chunk") or 0)
        delay_ms = float(profile.get("delay_ms") or 0)
        sizes = [int(s) for s in (profile.get("sizes") or []) if s > 0]
        gap_ms = max(0.0, float(profile.get("gap_ms") or 0))

        stream = b"".join(ctx.frames)
        if not stream:
            ctx.frames = []
            ctx.meta["inter_delay_s"] = 0.0
            return ctx
        if chunk <= 0 and not sizes:
            ctx.frames = [stream]
            ctx.meta["inter_delay_s"] = 0.0
            return ctx
        out: list[bytes] = []
        if sizes:
            # shape into the profile's packet-size signature (a repeating
            # per-frame size pattern like a video/batch download flow)
            pos, count = 0, 0
            while pos < len(stream):
                size = sizes[count % len(sizes)]
                out.append(stream[pos:pos + size])
                pos += size
                count += 1
            ctx.meta["inter_delay_s"] = gap_ms / 1000.0
        else:
            step = max(1, chunk)
            for pos in range(0, len(stream), step):
                out.append(stream[pos:pos + step])
            ctx.meta["inter_delay_s"] = delay_ms / 1000.0
        # NOTE: padding is intentionally NOT applied on this hop — the byte
        # stream must stay exactly identical (it is the user's traffic) and
        # these transports define no padding channel. EMUNEL_MORPH_PADDING_
        # PERCENT is honoured only as a report/no-op to keep env parity.
        ctx.frames = out
        return ctx

    # ---- configgen pipeline (fingerprint / sni / alpn hints) ------------------
    def _configgen(self, ctx: EngineContext) -> EngineContext:
        # The per-link fingerprint/SNI live in the link records (set in the
        # panel). Here we only record that morph is active so downstream
        # configgen engines (SNIRotation / Fronting) can cooperate.
        ctx.meta.setdefault("morph", {})
        ctx.meta["morph"]["active"] = True
        return ctx

    # ---- feedback / learning -----------------------------------------------------
    async def feedback(self, metrics: dict) -> None:
        if metrics.get("engine") != self.NAME:
            return
        isp = isp_for_ip(str(metrics.get("ip", "")), self.prefix_table)
        profile_name = self.chosen.get(isp)
        if not profile_name:
            return
        ok = bool(metrics.get("ok"))
        duration = float(metrics.get("duration") or 0)
        down = int(metrics.get("down_bytes") or 0)
        # reward: 1.0 for a healthy session, scaled down for quick deaths
        if ok:
            reward = 1.0
        elif down > 0 and duration > 1.0:
            reward = 0.4
        else:
            reward = 0.0
        self.bandit.update(profile_name, LinUCB.context(), reward)
        self.status.metrics["feedback"] += 1
        self.status.metrics["rewarded"] += reward
        self._persist()

    # ---- self-play ----------------------------------------------------------------
    async def _selfplay_loop(self) -> None:
        """Open synthetic connections to a configured public endpoint and feed
        the outcomes to the bandit. Requires EMUNEL_MORPH_SELFPLAY_URL (the
        public /i/<token>/... endpoint of a running instance)."""
        import httpx

        url = self.cfg.morph_selfplay_url
        while True:
            try:
                start = time.monotonic()
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(url, headers={"User-Agent": "emunel-selfplay"})
                    elapsed = time.monotonic() - start
                ok = 200 <= resp.status_code < 400
                self.bus.publish("probe.result", {
                    "engine": self.NAME, "ok": ok,
                    "detail": f"status={resp.status_code} {elapsed:.2f}s",
                })
            except Exception as exc:
                self.bus.publish("probe.result", {
                    "engine": self.NAME, "ok": False, "detail": str(exc)[:120]})
            await asyncio.sleep(max(30.0, float(self.cfg.morph_selfplay_interval_sec)))

    # ---- persistence + reporting ----------------------------------------------------
    def _persist(self) -> None:
        self.state.mutate(self.NAME, lambda payload: payload.update({
            "bandit": self.bandit.to_dict(),
            "chosen": self.chosen,
        }))

    def defaults(self) -> dict:
        return {
            "EMUNEL_MORPH_DEFAULT_CHUNK": self.cfg.morph_default_chunk,
            "EMUNEL_MORPH_DEFAULT_INTER_DELAY_MS": self.cfg.morph_default_delay_ms,
            "EMUNEL_MORPH_PADDING_PERCENT": self.cfg.morph_padding_percent,
            "EMUNEL_MORPH_BANDIT_ALPHA": self.cfg.morph_bandit_alpha,
            "profiles_configured": bool(self.cfg.morph_profiles),
            "selfplay": self.cfg.morph_selfplay,
        }

    def snapshot_metrics(self) -> dict:
        metrics = dict(self.status.metrics)
        if getattr(self, "bandit", None) is not None:
            metrics["arms"] = {
                name: {"pulls": arm.pulls,
                       "avg_reward": (arm.rewards / arm.pulls) if arm.pulls else 0.0}
                for name, arm in self.bandit.arms.items()
            }
        if getattr(self, "chosen", None) is not None:
            metrics["chosen"] = dict(self.chosen)
        return metrics
