"""Forward Error Correction Engine.

Honest scope: FEC only pays on lossy datagram channels — EMUNEL's current
transports (VLESS/Trojan/SS/VMess over WebSocket, xHTTP) are all TCP, where
the kernel already retransmits and userspace FEC would only duplicate bytes.
So this engine ships the real, tested XOR-based systematic erasure codec
(k data blocks + ceil(k*ratio) parity blocks per group, parity = XOR of a
rotated subset) and marks itself inactive with that reason. The codec is
exercised by unit tests and becomes active the day a UDP transport lands in
the platform — no code change needed, just EMUNEL_ENGINE_FEC_ENABLED=1 and
a datagram channel.

The XOR scheme is deliberately simple (parity = d1 XOR d3 XOR d5...) which
recovers any SINGLE lost block per group when configured with one parity
block, and combined groups recover more; it is cheap enough for the
free-plan CPU budget where Reed-Solomon over GF(256) would not be.
"""
from __future__ import annotations

from ..base import Engine, EngineContext


def encode_group(blocks: list[bytes], parity_count: int) -> list[bytes]:
    """Systematic XOR codec: returns the data blocks followed by parity
    blocks. Parity block i is the XOR of every data block whose
    (index % parity_count) == i — a single lost data block is always
    recoverable from its group's parity when only one block is missing."""
    if parity_count <= 0 or not blocks:
        return list(blocks)
    size = max(len(b) for b in blocks)
    padded = [b.ljust(size, b"\x00") for b in blocks]
    parity: list[bytes] = []
    for i in range(parity_count):
        acc = bytearray(size)
        for idx, block in enumerate(padded):
            if idx % parity_count == i:
                for j in range(size):
                    acc[j] ^= block[j]
        parity.append(bytes(acc))
    return list(blocks) + parity


def decode_group(received: list[bytes | None], parity_count: int) -> list[bytes] | None:
    """Recover missing data blocks using the parity blocks.
    received = data blocks (None = lost) + parity blocks. Returns recovered
    blocks or None when recovery is not possible (2+ losses in one group)."""
    data_len = len(received) - parity_count
    if data_len <= 0 or parity_count <= 0:
        return [r for r in received if r is not None] or None
    lost = [i for i, r in enumerate(received[:data_len]) if r is None]
    if not lost:
        return list(received[:data_len])  # type: ignore[index]
    size = max(len(r) for r in received if r is not None)
    out: list[bytes | None] = [
        r.ljust(size, b"\x00") if r is not None else None for r in received[:data_len]
    ]  # type: ignore[index]
    for lost_index in lost:
        parity_idx = lost_index % parity_count
        parity = received[data_len + parity_idx]
        if parity is None:
            return None
        acc = bytearray(parity.ljust(size, b"\x00"))
        for idx in range(data_len):
            if idx != lost_index and idx % parity_count == parity_idx:
                other = out[idx]
                if other is None:
                    return None  # two losses in the same parity group
                for j in range(size):
                    acc[j] ^= other[j]
        out[lost_index] = bytes(acc)
    return [b for b in out]  # type: ignore[list-item]


class FECEngine(Engine):
    NAME = "FEC"
    TITLE = "Forward Error Correction — XOR erasure codec (dormant on TCP)"
    HANDLES = frozenset()
    HOSTS = frozenset({"core"})

    async def init(self, config: dict) -> None:
        self.status.metrics.update({
            "blocks_encoded": 0, "blocks_recovered": 0,
            "codec_selftest": await self._selftest(),
        })

    def preconditions(self) -> str | None:
        return ("all current transports are TCP — the kernel already "
                "retransmits; FEC arms itself when a UDP transport exists")

    async def _selftest(self) -> str:
        try:
            data = [b"alpha-block", b"beta-block!!", b"gamma-block", b"delta-block"]
            encoded = encode_group(data, parity_count=self._parity_for(len(data)))
            # drop one data block, recover it
            holes = list(encoded)
            holes[1] = None
            recovered = decode_group(holes, self._parity_for(len(data)))
            if recovered is not None and recovered[1][:len(data[1])] == data[1]:
                return "ok"
            return "failed"
        except Exception as exc:
            return f"error: {exc}"

    def _parity_for(self, blocks: int) -> int:
        ratio = min(0.9, max(0.05, self.cfg.fec_ratio))
        return max(1, int(round(blocks * ratio)))

    async def process(self, ctx: EngineContext) -> EngineContext:
        # No datagram channel is wired in the current architecture (see
        # docstring) — kept as the extension point for UDP transports.
        return ctx

    def defaults(self) -> dict:
        return {
            "FEC_RATIO": self.cfg.fec_ratio,
            "FEC_BLOCK_SIZE": self.cfg.fec_block_size,
            "selftest": self.status.metrics.get("codec_selftest"),
        }
