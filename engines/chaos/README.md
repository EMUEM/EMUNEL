# Chaos Protocol (`engines/chaos/`)

An additive, self-contained engine implementing a **shape-shifting transport
framing**: the wire format rotates between HTTP/2-like, WebSocket-like,
gRPC-like and QUIC-like framing every **30–90 seconds** without dropping the
connection — both endpoints derive the identical schedule from wall-clock
time and a shared secret, so no negotiation is ever sent.

## How it works

| Spec item | Implementation |
|---|---|
| Shared seed | `HMAC-SHA256(CHAOS_SECRET, floor(now_ms / CHAOS_TICK_MS))` |
| State machine | `ChaosSession { frame_index, seed, switch_at, buffer }` |
| Frame rotation | `http2 → ws → grpc → quic` (seed-derived per tick) |
| Control frame | 4 bytes (`magic ‖ frame_index ‖ xor-checksum`) hidden at a seed-derived offset inside TLS-like record padding |
| Anti-stall | a shape never persists past 3 ticks → change guaranteed ≤ 90 s |
| Boundary tolerance | decoders retry with the previous window before failing |
| Stateless | the seed comes from the clock — a fresh session reconstructs the same schedule |

Budget targets honoured: every packet transform is one O(n) pass (CPU < 2 %
for 1000 sessions), the session registry is LRU-capped (default 1024 → RAM
well under 5 MB), and only standard-library modules are used
(`asyncio`, `hashlib`, `hmac`, `struct`, `secrets`).

## Wire format

```
b"\x17\x03\x03"  len:2 | padding(8..16B, seed-derived, contains the
                           4-byte control frame) | framed payload
```

Padding length, filler bytes and control offset are all seed-derived, so
encoder and decoder agree without exchanging a single byte.

## Honest scope

EMUNEL's public transports are WebSocket-carried streams served by the Core.
Rewriting live user traffic into chaos framing needs a matching client, so
the engine ships today as:

1. **the full protocol library** (`chaos_protocol.py`) — pure, unit-testable;
2. **loopback self-play** — a real TCP connection on 127.0.0.1 speaking
   chaos records both ways, proving integrity across shape switches
   (this is what `/api/chaos/status` reports and what the synergy cycle
   feeds back into DpiMesh);
3. **opt-in configgen hints** — when a request sets `meta["chaos"]`, the
   active genome's parameters (transport / cipher / fingerprint / MTU /
   padding / FEC ratio / compression / SNI strategy) are stamped into
   `meta["chaos_applied"]` for opt-in clients. **Without that flag the
   configgen pipeline is byte-identical to before** (snapshot test).

The Core data path is never touched.

## Flags & env

```
CHAOS_PROTOCOL_ENABLED=false      # engine flag (default OFF)
CHAOS_SECRET=change_me_please     # shared schedule secret
CHAOS_TICK_MS=30000               # base window (30s -> 30-90s changes)
CHAOS_MAX_SESSIONS=1024           # LRU session cap (RAM bound)
```

API: `GET /api/chaos/status` (admin) — window state, frames, self-play stats.
