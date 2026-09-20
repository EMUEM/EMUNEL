"""Packet Coalescing Engine.

Merges small downlink WS frames into bigger ones before they are written to
the client: fewer frames per byte -> less per-frame header overhead (WS
header + TCP/IP) and, for the Iranian DPI, traffic that looks like a
continuous download burst instead of a chatty small-packet stream.

Safety contract: the concatenated byte stream is preserved exactly — WS
message boundaries are soft for every EMUNEL transport. The middleware
provides batches (size/timeout from env); this engine decides how to merge.

Only the downlink direction is merged — the uplink carries interactive
traffic where latency matters more than overhead.
"""
from __future__ import annotations

from ..base import KIND_FRAMES, Engine, EngineContext


class CoalescingEngine(Engine):
    NAME = "Coalesce"
    TITLE = "Packet Coalescing — merge small downlink frames"
    HANDLES = frozenset({KIND_FRAMES})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        m = self.status.metrics
        m.update({"frames_in": 0, "frames_out": 0, "bytes_in": 0, "bytes_out": 0,
                  "merges": 0, "header_bytes_saved": 0})

    async def process(self, ctx: EngineContext) -> EngineContext:
        if ctx.direction != "down" or len(ctx.frames) <= 1:
            if ctx.direction == "down":
                self.status.metrics["frames_in"] += len(ctx.frames)
                self.status.metrics["frames_out"] += len(ctx.frames)
            return ctx
        m = self.status.metrics
        m["frames_in"] += len(ctx.frames)
        total = sum(len(f) for f in ctx.frames)
        m["bytes_in"] += total
        merged: list[bytes] = []
        current = bytearray()
        max_size = self.cfg.coalesce_max_size
        for frame in ctx.frames:
            if current and len(current) + len(frame) > max_size:
                merged.append(bytes(current))
                current = bytearray()
            current.extend(frame)
        if current:
            merged.append(bytes(current))
        # never grow a batch beyond the coalescing ceiling
        if sum(len(f) for f in merged) != total:
            merged = [b"".join(ctx.frames)] if total else []
        m["frames_out"] += len(merged)
        m["bytes_out"] += sum(len(f) for f in merged)
        m["merges"] += max(0, len(ctx.frames) - len(merged))
        # 4 bytes is the minimum WS client->server frame header we avoid per
        # merged message (masked frames carry a 4-byte mask too).
        m["header_bytes_saved"] += 8 * max(0, len(ctx.frames) - len(merged))
        ctx.frames = merged
        return ctx

    def defaults(self) -> dict:
        return {
            "MAX_COALESCE_SIZE": self.cfg.coalesce_max_size,
            "COALESCE_TIMEOUT_MS": self.cfg.coalesce_timeout_ms,
            "EMUNEL_ENGINE_COALESCE_MAX_BUFFER": self.cfg.coalesce_max_buffer,
        }
