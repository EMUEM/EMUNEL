"""PayloadEngine — merged Group D (STABILIZATION stage 2.1 + 3.2).

  Compression + Coalescing   ("Dedup" does not exist as an engine in this
  codebase — the group wraps what is real; documented in STABILIZATION.md)

Resource shape (spec 3.2): low compression effort (Brotli/zlib level 4
instead of 6) and only bodies worth it (>1KB via
EMUNEL_HTTP_COMPRESS_MIN_BYTES=1024 on the console hop). Coalescing keeps
its 8ms window; the 16KB per-stream buffer cap (BufferPool) is the group's
backpressure bound — with coalescing disabled mid-flight the frames flow
through uncompressed, never buffered whole.
"""
from __future__ import annotations

import dataclasses

from .facade import ConsolidatedEngine


class PayloadEngine(ConsolidatedEngine):
    NAME = "Payload"
    TITLE = ("Payload Optimization — Compression + Coalescing "
             "(merged Group D)")
    GROUP = "payload"
    CHILDREN = ("Coalesce", "Compress")
    PIPELINE_VAR = "EMUNEL_PAYLOAD_PIPELINE"

    MERGED_COMPRESS_LEVEL = 4      # spec 3.2: Brotli 4 / Zstd 3 territory

    def _child_cfg(self, name: str):
        if name == "Compress":
            return dataclasses.replace(
                self.cfg,
                compress_level=min(self.MERGED_COMPRESS_LEVEL,
                                    self.cfg.compress_level))
        if name == "Coalesce":
            # spec 3.3: 16KB max buffered per stream — the coalescer flushes
            # when a stream's batch would exceed it (streaming, not buffering)
            return dataclasses.replace(
                self.cfg,
                coalesce_max_buffer=min(16 * 1024, self.cfg.coalesce_max_buffer))
        return self.cfg

    def defaults(self) -> dict:
        out = super().defaults()
        out["compress_level"] = self.MERGED_COMPRESS_LEVEL
        out["min_http_bytes_hint"] = 1024
        return out
