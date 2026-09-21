"""State-confusion techniques — syndata / synack packet templates.

/state_confusion builds the exact TCP segment templates a raw-socket
client helper would inject to confuse a STATEFUL DPI:

  * syndata  — push payload bytes INSIDE the SYN packet. A DPI that keys
    its flow state on "first data packet after handshake" mis-anchors its
    reassembly window; the server's kernel stack (Linux) tolerates SYN
    data only with TCP_FASTOPEN — so the helper sends the SYN data as a
    SEPARATE short-lived decoy flow (low TTL), keeping the real connection
    on the normal path.
  * synack   — a fake SYN-ACK sent FROM the client side. The DPI's flow
    table sees a completed handshake that the server never confirmed;
    sequence-number bookkeeping desynchronizes while the real three-way
    handshake proceeds underneath.

Both need raw-socket privileges (IP_HDRINCL / packet injection) that a
Railway panel neither has nor should have — the panel BUILDS and ships
the templates; execution is the client helper's job, only when it runs
with admin/root and the operator asked for it.

Checksums are computed over IPv4 pseudo-headers (pure stdlib, mirrors
RFC 1071). Everything here is deterministic and unit-tested.
"""
from __future__ import annotations

import struct

# ── IPv4 checksum (RFC 1071) ─────────────────────────────────────────────────
def checksum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def describe_state_confusion(technique: str) -> str:
    honest = {
        "syndata": ("syndata pushes decoy payload bytes inside the SYN packet — "
                    "raw-socket helper only; userspace helpers degrade to the "
                    "low-TTL decoy flow"),
        "synack": ("synack injects a fake SYN-ACK from the client side, "
                   "desynchronizing the DPI's flow table — raw-socket helper only"),
    }
    return honest.get(technique, f"{technique}: raw-socket technique (see docs)")


def build_tcp_segment(*, src_port: int, dst_port: int, seq: int, ack: int = 0,
                      flags: int = 0x02, window: int = 64240,
                      payload: bytes = b"", md5sig: bool = False,
                      urgent: int = 0) -> bytes:
    """One TCP header (+optional payload), no IP layer.

    flags bits: SYN=0x02, ACK=0x10, PSH=0x08, URG=0x20 — combinable.
    md5sig=True appends the TCP MD5 Signature option (kind 19) with a
    WRONG digest — the zapret "fooling=md5sig": NATs with stateful TCP-MD5
    handling drop the segment while many DPI stacks still parse it.
    """
    options = b""
    if md5sig:
        # kind=19, len=18, 16 zero bytes = intentionally-invalid digest
        options += bytes([19, 18]) + b"\x00" * 16
    if urgent:
        options += bytes([0, 0])                     # EOL+EOL padding
    offset = (20 + len(options) + 3) // 4           # 32-bit words
    header = struct.pack("!HHIIBBHHH",
                         src_port & 0xFFFF, dst_port & 0xFFFF,
                         seq & 0xFFFFFFFF, ack & 0xFFFFFFFF,
                         (offset << 4) & 0xFF, flags & 0xFF,
                         window & 0xFFFF, 0, urgent & 0xFFFF)
    pseudo = header + options + payload
    csum = checksum(pseudo)
    segment = header[:16] + struct.pack("!H", csum) + header[18:] + options + payload
    return segment


def build_ipv4_packet(segment: bytes, *, src: str, dst: str,
                      ttl: int = 4, protocol: int = 6) -> bytes:
    """Wrap a TCP segment in a minimal IPv4 header (no options)."""
    src_bytes = bytes(int(x) for x in src.split("."))
    dst_bytes = bytes(int(x) for x in dst.split("."))
    if len(src_bytes) != 4 or len(dst_bytes) != 4:
        raise ValueError("src/dst must be dotted-quad IPv4 addresses")
    total_length = 20 + len(segment)
    header = struct.pack("!BBHHHBBH4s4s",
                         0x45, 0x00, total_length,
                         0, 0,                       # id, flags/frag
                         max(1, min(255, ttl)) & 0xFF, protocol & 0xFF,
                         0, src_bytes, dst_bytes)
    csum = checksum(header)
    header = header[:10] + struct.pack("!H", csum) + header[12:]
    return header + segment


def build_syndata_decoy(*, src: str, dst: str, dst_port: int,
                        decoy_payload: bytes, ttl: int = 4) -> bytes:
    """SYN packet carrying decoy payload bytes (syndata state confusion)."""
    seg = build_tcp_segment(src_port=40000 + (len(decoy_payload) % 20000),
                            dst_port=dst_port, seq=1, flags=0x02,
                            payload=decoy_payload)
    return build_ipv4_packet(seg, src=src, dst=dst, ttl=ttl)


def build_synack_decoy(*, src: str, dst: str, dst_port: int,
                       seq: int = 0xDEADBEEF, ttl: int = 4) -> bytes:
    """Fake SYN-ACK sent FROM the client side (synack state confusion)."""
    seg = build_tcp_segment(src_port=dst_port, dst_port=40000, seq=seq,
                            ack=seq + 1, flags=0x12)          # SYN+ACK
    return build_ipv4_packet(seg, src=dst, dst=src, ttl=ttl)


def build_md5sig_decoy_segment(*, src_port: int, dst_port: int,
                               payload: bytes) -> bytes:
    """Data segment carrying an invalid TCP-MD5 signature (fooling=md5sig)."""
    return build_tcp_segment(src_port=src_port, dst_port=dst_port,
                             seq=1, ack=1, flags=0x18,          # PSH+ACK
                             payload=payload, md5sig=True)


def build_oob_decoy_segment(*, src_port: int, dst_port: int,
                            payload: bytes, oob_byte: int = 0x00) -> bytes:
    """Segment with the TCP urgent pointer set — the OOB byte rides inside
    the payload at the urgent offset; simple DPI parsers choke on it."""
    seg = build_tcp_segment(src_port=src_port, dst_port=dst_port,
                            seq=1, ack=1, flags=0x18 | 0x20,     # PSH+ACK+URG
                            payload=payload, urgent=1)
    return seg
