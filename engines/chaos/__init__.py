"""Chaos Protocol engine package (additive, flag-gated off by default)."""
from .chaos_protocol import (
    FRAMES, ChaosFrameError, ChaosProtocol, ChaosSession, ChaosWindow,
    decode_packet, derive_seed, encode_packet, window_at,
)
from .engine import ChaosEngine

__all__ = [
    "ChaosEngine", "ChaosProtocol", "ChaosSession", "ChaosWindow",
    "ChaosFrameError", "FRAMES", "derive_seed", "encode_packet",
    "decode_packet", "window_at",
]
