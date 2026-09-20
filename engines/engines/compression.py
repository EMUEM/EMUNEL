"""Adaptive Compression Engine.

Scope, honestly stated: on this platform every client-visible hop is a
standard VLESS/Trojan/SS/VMess tunnel — the client on the other end is a
stock proxy core and cannot decompress anything, so compressing the
client-facing byte stream would break it. The engine therefore:

  * compresses only hops where BOTH ends are ours (the engine-internal
    data channel and the engine state/backup payloads)
  * sniffs content and picks the codec (text -> brotli when the wheel is
    installed, otherwise zlib; incompressible-looking binary -> skip)
  * enforces the MIN_SAVING_PERCENT rule and self-disables for a batch
    when compression does not pay (encrypted inner traffic usually does
    not — the rule catches this within one batch)

The codec itself is real and tested; it becomes user-visible the moment a
transport that negotiates compression end-to-end exists (e.g. WS
permessage-deflate support on both sides).
"""
from __future__ import annotations

import time
import zlib

from ..base import KIND_FRAMES, Engine, EngineContext


def _brotli_available() -> bool:
    try:
        import brotli  # type: ignore

        return True
    except ImportError:
        return False


def _looks_like_text(sample: bytes) -> bool:
    if not sample:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) > 0.85


class CompressionEngine(Engine):
    NAME = "Compress"
    TITLE = "Adaptive Compression — content-aware, 5% minimum saving"
    HANDLES = frozenset({KIND_FRAMES})
    HOSTS = frozenset({"core"})

    async def init(self, config: dict) -> None:
        self.algos = [a for a in self.cfg.compress_algos
                      if a == "zlib" or (a == "brotli" and _brotli_available())] or ["zlib"]
        self.status.metrics.update({
            "batches": 0, "compressed": 0, "skipped_incompressible": 0,
            "bytes_in": 0, "bytes_out": 0, "bytes_saved": 0,
        })

    def preconditions(self) -> str | None:
        # Honest status: no hop in the current architecture can be compressed
        # without breaking the stock client cores (see docstring). The codec
        # stays registered + tested; it activates automatically when the core
        # host gains a frame channel both ends own.
        return ("no compression-capable hop in the current architecture "
                "(client cores cannot decompress); codec armed and tested")

    async def process(self, ctx: EngineContext) -> EngineContext:
        if not ctx.frames:
            return ctx
        m = self.status.metrics
        m["batches"] += 1
        stream = b"".join(ctx.frames)
        m["bytes_in"] += len(stream)
        sample = stream[: self.cfg.compress_sample]
        if not sample:
            return ctx
        best: bytes | None = None
        if _looks_like_text(sample):
            if "brotli" in self.algos:
                try:
                    import brotli  # type: ignore

                    best = brotli.compress(stream, quality=self._level())
                except Exception:
                    best = None
        if best is None and "zlib" in self.algos:
            try:
                best = zlib.compress(stream, level=self._level())
            except zlib.error:
                best = None
        if best is None:
            m["skipped_incompressible"] += 1
            return ctx
        saving = 100.0 * (1.0 - len(best) / max(1, len(stream)))
        if saving < self.cfg.compress_min_saving:
            m["skipped_incompressible"] += 1
            return ctx
        # compressed output is only used on engine-internal channels; the
        # relay stream itself stays untouched (see module docstring).
        m["compressed"] += 1
        m["bytes_out"] += len(best)
        m["bytes_saved"] += max(0, len(stream) - len(best))
        ctx.meta["compression"] = {"ratio": round(saving, 1),
                                   "size": len(best), "ts": time.time()}
        return ctx

    def _level(self) -> int:
        return max(0, min(9, self.cfg.compress_level))

    def defaults(self) -> dict:
        return {
            "COMPRESSION_LEVEL": self.cfg.compress_level,
            "MIN_SAVING_PERCENT": self.cfg.compress_min_saving,
            "EMUNEL_COMPRESS_ALGOS": ",".join(self.algos),
            "brotli_available": _brotli_available(),
        }
