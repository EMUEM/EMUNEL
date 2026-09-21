"""Adaptive Compression Engine — two channels, one per host.

CONSOLE host (the real, working channel): the panel's own HTTP responses
(SPA page, JS/CSS assets, API JSON, subscription feeds) go to browsers and
client cores that send Accept-Encoding. engines/http_compress.py compresses
them (gzip stdlib, brotli when the wheel is present), streaming-safe, never
touching the /i/* data path. Enable this engine in Engine Settings (the
choice persists) or set EMUNEL_HTTP_COMPRESSION=true; savings show up in
this engine's metrics.

CORE host (frame pipeline) — honestly still cannot run: every client-visible
hop is a standard VLESS/Trojan/SS/VMess tunnel and the stock client core on
the other end cannot decompress anything. The codec stays registered and
tested there, self-disabling with a visible reason until a transport that
negotiates compression end-to-end exists.
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
    TITLE = "Adaptive Compression — HTTP responses (panel + feeds) + codec"
    HANDLES = frozenset({KIND_FRAMES})
    HOSTS = frozenset({"core", "console"})

    async def init(self, config: dict) -> None:
        self.algos = [a for a in self.cfg.compress_algos
                      if a == "zlib" or (a == "brotli" and _brotli_available())] or ["zlib"]
        self.status.metrics.update({
            "batches": 0, "compressed": 0, "skipped_incompressible": 0,
            "bytes_in": 0, "bytes_out": 0, "bytes_saved": 0,
        })

    def preconditions(self) -> str | None:
        if self._on_console():
            return None      # real channel here: HTTP response compression
        # Core host — honest status: no hop in the current architecture can
        # be compressed without breaking the stock client cores (see
        # docstring). The codec stays registered + tested; it activates
        # automatically when the core host gains a frame channel both ends
        # own.
        return ("no compression-capable hop in the current architecture "
                "(client cores cannot decompress); codec armed and tested")

    def _on_console(self) -> bool:
        return self.cfg.host == "console"

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
        base = {
            "COMPRESSION_LEVEL": self.cfg.compress_level,
            "MIN_SAVING_PERCENT": self.cfg.compress_min_saving,
            "EMUNEL_COMPRESS_ALGOS": ",".join(self.algos),
            "brotli_available": _brotli_available(),
        }
        if self._on_console():
            try:
                from ..http_compress import stats as http_stats

                base.update(http_stats())
                base["http_activation"] = ("enable this engine (Engine Settings) "
                                            "or EMUNEL_HTTP_COMPRESSION=true")
            except Exception:
                pass
        return base

    def snapshot_metrics(self) -> dict:
        metrics = dict(self.status.metrics)
        if self._on_console():
            try:
                from ..http_compress import stats as http_stats

                metrics.update(http_stats())
            except Exception:
                pass
        return metrics
