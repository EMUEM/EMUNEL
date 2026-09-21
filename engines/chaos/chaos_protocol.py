"""Chaos Protocol — a shape-shifting transport framing (additive engine).

Concept (operator spec): a protocol that changes its wire shape every
30-90 seconds WITHOUT dropping the connection. Both endpoints derive the
same schedule from wall-clock time + a shared secret, so no negotiation
frames are ever needed.

Mapping of the spec (written for Node.js) onto this Python codebase:

  * net / crypto / events native modules  ->  asyncio + hashlib/hmac +
    standard-library types only. No third-party dependency is introduced.
  * Seed: HMAC-SHA256(CHAOS_SECRET, floor(Date.now()/30000))
         -> derive_seed() below (bucket = floor(now_ms / tick_ms)).
  * State machine { frameIndex, seed, switchAt, buffer }
         -> ChaosSession (per-connection cache + partial-record buffer).
  * Frame rotation HTTP/2 -> WS -> gRPC -> QUIC-like
         -> FRAMES + the four codecs in this module.
  * Control frame: 4 bytes hidden in the padding of TLS-like records
         -> CONTROL_MAGIC + encode_packet()/decode_packet().
  * Stateless: the seed is derived from time, so a fresh session
    reconstructs the exact same schedule as its peer (session fields are
    a cache, not shared state).
  * Budget targets: CPU < 2% and RAM < 5MB for 1000 sessions — the
    protocol registry caps live sessions (LRU eviction) and every packet
    transform is a single O(n) pass over the payload.

Wire format (one "chaos record"):

    b"\\x17\\x03\\x03"  len:2  |  padding (8..16B, seed-derived,
                                      holds the 4-byte control frame
                                      at a seed-derived offset)
                             |  framed payload (http2 | ws | grpc | quic)

The padding bytes, their length and the control offset are all derived
from the window seed — encoder and decoder agree without any handshake.
Decoders additionally retry with the PREVIOUS window's parameters so a
record encoded right before a switch still decodes (tolerant boundary).

Honest scope: EMUNEL's public transports are WebSocket-carried streams
served by the Core; rewriting live user traffic into chaos framing would
require a matching client. The engine therefore (a) ships the full
protocol library, (b) proves it over real loopback TCP (self-play), and
(c) exposes genome-derived hints through the configgen pipeline for
opt-in clients. The Core data path is never touched.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass

__all__ = [
    "ChaosFrameError", "ChaosProtocol", "ChaosSession", "ChaosWindow",
    "FRAMES", "derive_seed", "encode_packet", "decode_packet", "window_at",
]

# Frame rotation order — the "shape" the protocol cycles through.
FRAMES = ("http2", "ws", "grpc", "quic")

TICK_MS_DEFAULT = 30_000        # base window: the spec's floor(now/30000)
MAX_RUN = 3                      # anti-stall: a shape never persists past
                                 # 3 ticks (30s tick -> change within 90s)
CONTROL_MAGIC = bytes((0xE1, 0x17))   # appdata marker inside the padding
TLS_LIKE_HEADER = b"\x17\x03\x03"     # TLS 1.2 application_data record
MAX_PACKET = 262_144             # hard cap per encoded packet (256 KiB)
DEFAULT_MAX_SESSIONS = 1024      # RAM bound (< 5MB of session state)


class ChaosFrameError(Exception):
    """A record failed structural verification (wrong window / corrupt)."""


# ── shared schedule ─────────────────────────────────────────────────────────

def derive_seed(secret: str | bytes, bucket: int) -> bytes:
    """HMAC-SHA256(secret, bucket) — the shared per-window seed.

    bucket = floor(now_ms / tick_ms): both sides compute the same seed from
    wall-clock time alone, so the schedule is stateless by construction.
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, str(int(bucket)).encode("ascii"),
                    hashlib.sha256).digest()


def _frame_index(secret: str | bytes, bucket: int) -> int:
    """Frame for a bucket, with the anti-stall rule.

    A raw run of 3+ identical buckets would freeze one shape; when that
    happens the forced value depends on the bucket itself, so consecutive
    forced values differ — the effective hold of any shape is bounded at
    3 ticks (30s tick -> a change is guaranteed within 90s)."""
    idx = derive_seed(secret, bucket)[0] % len(FRAMES)
    prev = derive_seed(secret, bucket - 1)[0] % len(FRAMES)
    prev2 = derive_seed(secret, bucket - 2)[0] % len(FRAMES)
    if idx == prev == prev2:
        idx = (idx + 1 + bucket) % len(FRAMES)
    return idx


@dataclass(frozen=True)
class ChaosWindow:
    """One shape window: which frame is on the wire and until when."""
    bucket: int
    tick_ms: int
    frame_index: int
    frame_name: str
    seed: bytes
    started_at_ms: int
    switch_at_ms: int

    @property
    def seed_hex(self) -> str:
        return self.seed.hex()

    def switch_in_ms(self, now_ms: int) -> int:
        return max(0, self.switch_at_ms - int(now_ms))


def window_at(secret: str | bytes, tick_ms: int, now_ms: int) -> ChaosWindow:
    """The deterministic window covering now_ms."""
    tick = max(100, int(tick_ms))
    bucket = int(now_ms) // tick
    seed = derive_seed(secret, bucket)
    frame_index = _frame_index(secret, bucket)
    # switch_at: the next tick boundary whose frame differs (<= MAX_RUN
    # ticks out, enforced by the anti-stall rule above)
    step = 1
    while step <= MAX_RUN and _frame_index(secret, bucket + step) == frame_index:
        step += 1
    return ChaosWindow(
        bucket=bucket, tick_ms=tick,
        frame_index=frame_index, frame_name=FRAMES[frame_index],
        seed=seed, started_at_ms=bucket * tick,
        switch_at_ms=(bucket + step) * tick,
    )


# ── frame codecs (HTTP/2 / WS / gRPC / QUIC-like) ─────────────────────────

def _enc_http2(payload: bytes) -> bytes:
    # 9-byte DATA framing: len:3 type:1 flags:1 stream:4
    return (len(payload).to_bytes(3, "big") + b"\x00\x00"
            + (1).to_bytes(4, "big") + payload)


def _dec_http2(framed: bytes) -> bytes:
    if len(framed) < 9 or framed[3] != 0x00:
        raise ChaosFrameError("bad http2 frame header")
    length = int.from_bytes(framed[0:3], "big")
    if len(framed) - 9 < length:
        raise ChaosFrameError("http2 length overrun")
    return framed[9:9 + length]


def _enc_ws(payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        head = bytes((0x82, n))                       # FIN + binary opcode
    elif n < 65536:
        head = bytes((0x82, 126)) + struct.pack(">H", n)
    else:
        head = bytes((0x82, 127)) + struct.pack(">Q", n)
    return head + payload


def _dec_ws(framed: bytes) -> bytes:
    if len(framed) < 2 or framed[0] != 0x82:
        raise ChaosFrameError("bad ws frame header")
    n, off = framed[1], 2
    if n == 126:
        if len(framed) < 4:
            raise ChaosFrameError("ws extended length truncated")
        n, off = int.from_bytes(framed[2:4], "big"), 4
    elif n == 127:
        if len(framed) < 10:
            raise ChaosFrameError("ws jumbo length truncated")
        n, off = int.from_bytes(framed[2:10], "big"), 10
    if len(framed) - off < n:
        raise ChaosFrameError("ws length overrun")
    return framed[off:off + n]


def _enc_grpc(payload: bytes) -> bytes:
    # 5-byte length-prefixed message: compressed-flag + len:4
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _dec_grpc(framed: bytes) -> bytes:
    if len(framed) < 5 or framed[0] != 0x00:
        raise ChaosFrameError("bad grpc message prefix")
    n = struct.unpack(">I", framed[1:5])[0]
    if len(framed) - 5 < n:
        raise ChaosFrameError("grpc length overrun")
    return framed[5:5 + n]


def _enc_quic(payload: bytes) -> bytes:
    # QUIC-like short header: type byte + u16 length (payload capped 64 KiB)
    return b"\x40" + struct.pack(">H", min(len(payload), 65535)) + payload


def _dec_quic(framed: bytes) -> bytes:
    if len(framed) < 3 or framed[0] != 0x40:
        raise ChaosFrameError("bad quic-like header")
    n = struct.unpack(">H", framed[1:3])[0]
    if len(framed) - 3 < n:
        raise ChaosFrameError("quic-like length overrun")
    return framed[3:3 + n]


_ENCODERS = (_enc_http2, _enc_ws, _enc_grpc, _enc_quic)
_DECODERS = (_dec_http2, _dec_ws, _dec_grpc, _dec_quic)


# ── record layer: TLS-like wrapper + control frame in the padding ──────────

@dataclass(frozen=True)
class _PaddingPlan:
    length: int
    offset: int


def _padding_plan(seed: bytes) -> _PaddingPlan:
    """Padding length (8..16B) and control-frame offset, both seed-derived
    so encoder and decoder agree without exchanging anything."""
    length = 8 + (seed[3] % 9)
    offset = seed[4] % (length - 3)      # control fits: offset + 4 <= length
    return _PaddingPlan(length=length, offset=offset)


def _pad_stream(seed: bytes, length: int) -> bytes:
    """Deterministic padding filler derived from the window seed."""
    out = bytearray()
    block = 0
    while len(out) < length:
        out += hashlib.sha256(seed + b":pad:" + str(block).encode("ascii")).digest()
        block += 1
    return bytes(out[:length])


def _control(frame_index: int) -> bytes:
    # 4 bytes: magic(2) + frame index(1) + xor checksum(1)
    return bytes((CONTROL_MAGIC[0], CONTROL_MAGIC[1], frame_index & 0xFF,
                  CONTROL_MAGIC[0] ^ CONTROL_MAGIC[1]
                  ^ (frame_index & 0xFF) ^ 0xC7))


def encode_packet(payload: bytes, win: ChaosWindow) -> bytes:
    """Wrap one payload into a chaos record for the given window."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    payload = bytes(payload)
    if len(payload) > MAX_PACKET:
        raise ChaosFrameError("payload exceeds the chaos packet cap")
    framed = _ENCODERS[win.frame_index % len(FRAMES)](payload)
    plan = _padding_plan(win.seed)
    padding = bytearray(_pad_stream(win.seed, plan.length))
    padding[plan.offset:plan.offset + 4] = _control(win.frame_index)
    inner = bytes(padding) + framed
    if len(inner) > 65535:
        raise ChaosFrameError("chaos record exceeds 64 KiB")
    return TLS_LIKE_HEADER + struct.pack(">H", len(inner)) + inner


def decode_packet(record: bytes, win: ChaosWindow) -> bytes:
    """Verify + unwrap one chaos record that was encoded for `win`."""
    if len(record) < 5 or record[:3] != TLS_LIKE_HEADER:
        raise ChaosFrameError("not a chaos TLS-like record")
    (inner_len,) = struct.unpack(">H", record[3:5])
    if len(record) - 5 < inner_len:
        raise ChaosFrameError("truncated chaos record")
    inner = record[5:5 + inner_len]
    plan = _padding_plan(win.seed)
    if plan.length + 1 > len(inner):
        raise ChaosFrameError("padding shorter than the window plan")
    control = inner[plan.offset:plan.offset + 4]
    expected = _control(win.frame_index)
    if control != expected:
        raise ChaosFrameError("control frame mismatch — different window")
    framed = inner[plan.length:]
    if not framed:
        raise ChaosFrameError("chaos record carries no payload")
    return _DECODERS[win.frame_index % len(FRAMES)](framed)


# ── per-connection state machine ───────────────────────────────────────────

class ChaosSession:
    """{ frame_index, seed, switch_at, buffer } for one connection.

    Purely a cache over the time-derived schedule plus a partial-record
    buffer — nothing here is shared with the peer, which reconstructs the
    identical schedule from the clock and CHAOS_SECRET.
    """

    __slots__ = ("proto", "frame_index", "seed", "seed_hex", "switch_at_ms",
                 "buffer", "_bucket", "_win", "packets_in", "packets_out",
                 "switches", "errors", "frames_seen")

    def __init__(self, proto: "ChaosProtocol"):
        self.proto = proto
        self.buffer = b""
        self._bucket = -1
        self._win: ChaosWindow | None = None
        self.packets_in = 0
        self.packets_out = 0
        self.switches = 0
        self.errors = 0
        self.frames_seen: set[str] = set()
        now = time.time() * 1000.0
        win = self._window(now)
        self.frame_index = win.frame_index
        self.seed = win.seed
        self.seed_hex = win.seed.hex()
        self.switch_at_ms = win.switch_at_ms
        self.frames_seen.add(win.frame_name)

    # ---- schedule cache ----------------------------------------------------
    def _window(self, now_ms: float) -> ChaosWindow:
        bucket = int(now_ms) // self.proto.tick_ms
        if self._win is None or self._bucket != bucket:
            self._win = window_at(self.proto.secret, self.proto.tick_ms,
                                  int(now_ms))
            self._bucket = bucket
        return self._win

    def _advance(self, now_ms: float) -> ChaosWindow:
        win = self._window(now_ms)
        if win.frame_index != self.frame_index:
            self.switches += 1
            self.frames_seen.add(win.frame_name)
        self.frame_index = win.frame_index
        self.seed = win.seed
        self.seed_hex = win.seed.hex()
        self.switch_at_ms = win.switch_at_ms
        return win

    # ---- data path ----------------------------------------------------------
    def encode(self, payload: bytes, now_ms: float | None = None) -> bytes:
        now = time.time() * 1000.0 if now_ms is None else now_ms
        win = self._advance(now)
        record = encode_packet(payload, win)
        self.packets_out += 1
        return record

    def decode(self, data: bytes, now_ms: float | None = None) -> bytes | None:
        """Feed received bytes; returns a payload once a full record is in
        (partial records stay buffered), or None while still incomplete.

        Boundary tolerance: if the record fails verification under the
        current window (the peer encoded just before a switch), the
        previous window is tried before giving up.
        """
        now = time.time() * 1000.0 if now_ms is None else now_ms
        self.buffer = self.buffer + bytes(data) if self.buffer else bytes(data)
        while True:
            if len(self.buffer) < 5:
                return None
            if self.buffer[:3] != TLS_LIKE_HEADER:
                # resynchronise on the next plausible record start
                idx = self.buffer.find(TLS_LIKE_HEADER, 1)
                if idx < 0:
                    self.buffer = b""
                    return None
                self.buffer = self.buffer[idx:]
                continue
            (inner_len,) = struct.unpack(">H", self.buffer[3:5])
            if len(self.buffer) - 5 < inner_len:
                return None                     # keep buffering
            record = self.buffer[:5 + inner_len]
            self.buffer = self.buffer[5 + inner_len:]
            self.packets_in += 1
            win = self._advance(now)
            try:
                return decode_packet(record, win)
            except ChaosFrameError:
                prev = window_at(self.proto.secret, self.proto.tick_ms,
                                 int(now) - self.proto.tick_ms)
                try:
                    return decode_packet(record, prev)
                except ChaosFrameError:
                    self.errors += 1
                    raise

    def describe(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "frame": FRAMES[self.frame_index],
            "seed": self.seed_hex[:16],
            "switch_at_ms": self.switch_at_ms,
            "buffer_bytes": len(self.buffer),
            "switches": self.switches,
            "packets_in": self.packets_in,
            "packets_out": self.packets_out,
            "errors": self.errors,
        }


# ── protocol registry + loopback proof ─────────────────────────────────────

class ChaosProtocol:
    """Session registry + the loopback self-play that proves the protocol
    over a real TCP connection (used by /api/chaos/status and the synergy
    feedback cycle)."""

    def __init__(self, secret: str | None = None, *,
                 tick_ms: int = TICK_MS_DEFAULT,
                 max_sessions: int = DEFAULT_MAX_SESSIONS):
        self.secret = secret or "change_me_please"
        self.tick_ms = max(100, int(tick_ms))
        self.max_sessions = max(8, int(max_sessions))
        self._sessions: OrderedDict[str, ChaosSession] = OrderedDict()
        self.self_plays = 0

    # ---- sessions ------------------------------------------------------------
    def session(self, session_id: str | None = None) -> tuple[str, ChaosSession]:
        sid = session_id or secrets.token_urlsafe(8)
        sess = self._sessions.get(sid)
        if sess is None:
            sess = ChaosSession(self)
            self._sessions[sid] = sess
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)      # LRU — RAM bound
        else:
            self._sessions.move_to_end(sid)
        return sid, sess

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def window_now(self) -> ChaosWindow:
        return window_at(self.secret, self.tick_ms, int(time.time() * 1000))

    def stats(self) -> dict:
        win = self.window_now()
        return {
            "tick_ms": self.tick_ms,
            "frames": list(FRAMES),
            "current_frame": win.frame_name,
            "current_seed": win.seed_hex[:16],
            "switch_in_ms": win.switch_in_ms(int(time.time() * 1000)),
            "sessions": len(self._sessions),
            "max_sessions": self.max_sessions,
            "self_plays": self.self_plays,
        }

    # ---- loopback proof --------------------------------------------------------
    async def self_play(self, rounds: int = 8, payload_size: int = 256,
                        interval_s: float = 0.05) -> dict:
        """Real TCP connection (127.0.0.1) speaking chaos records both ways.

        Returns integrity + timing evidence: rounds, ok, rtt_avg_ms,
        rtt_jitter_ms, switches, frames — consumed by Test 2 (Chaos only)
        and the synergy feedback loop."""
        self.self_plays += 1
        results: dict = {"rounds": 0, "ok": 0, "errors": 0, "switches": 0,
                        "frames": [], "rtt_avg_ms": None, "rtt_jitter_ms": None}

        async def _serve(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
            _, server_sess = self.session("selfplay-server")
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    payload = server_sess.decode(data)
                    if payload is not None:
                        ack = b"ack:" + hashlib.sha256(payload).digest()[:8]
                        writer.write(server_sess.encode(ack))
                        await writer.drain()
            except (ChaosFrameError, ConnectionError):
                results["errors"] += 1
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:                       # noqa: BLE001
                    pass

        server = await asyncio.start_server(_serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _, client_sess = self.session("selfplay-client")
        rtts: list[float] = []
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for i in range(max(1, int(rounds))):
                payload = (hashlib.sha256(f"chaos-round-{i}".encode()).digest()
                           * max(1, int(payload_size) // 32))
                results["rounds"] += 1
                t0 = time.perf_counter()
                writer.write(client_sess.encode(payload))
                await writer.drain()
                echo = await asyncio.wait_for(reader.read(65536), 5.0)
                back = client_sess.decode(echo)
                rtts.append((time.perf_counter() - t0) * 1000.0)
                if back == b"ack:" + hashlib.sha256(payload).digest()[:8]:
                    results["ok"] += 1
                else:
                    results["errors"] += 1
                if interval_s > 0:
                    await asyncio.sleep(interval_s)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:                           # noqa: BLE001
                pass
        finally:
            server.close()
            await server.wait_closed()

        if rtts:
            avg = sum(rtts) / len(rtts)
            var = sum((r - avg) ** 2 for r in rtts) / len(rtts)
            results["rtt_avg_ms"] = round(avg, 3)
            results["rtt_jitter_ms"] = round(var ** 0.5, 3)
        results["switches"] = client_sess.switches
        results["frames"] = sorted(client_sess.frames_seen)
        results["integrity"] = results["ok"] == results["rounds"]
        return results
