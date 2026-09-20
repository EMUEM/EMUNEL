#!/usr/bin/env python3
"""EMUNEL SNI Bypass Helper — runs ON THE USER'S DEVICE (client side).

SNI spoofing, ClientHello fragmentation and the TTL trick must execute
between the user's apps and the DPI box — i.e. on this device, never on
the Railway server. Point your browser/proxy at the local listener this
script opens, and it will:

  1. accept a TCP connection locally (default 127.0.0.1:40443);
  2. connect to the real server (your EMUNEL edge / Railway TCP proxy);
  3. intercept the first ClientHello;
  4. apply the bypass method:
       fragment   split the real ClientHello (strategy below)
       fake_sni   send a synthetic "allowed" ClientHello on a separate
                  low-TTL connection first (dies mid-path; DPI sees it,
                  the server never does)
       combined   fake packet first, then fragmented real hello
  5. relay both directions afterwards.

Fragment strategies: sni_split (cut inside the SNI hostname), half,
multi (~24-byte pieces), tls_record_frag (handshake split across two
TLS records).

Usage:
    python3 emunel_sni_helper.py --connect your-server.example.com:443 \
        --method combined --strategy sni_split --delay 0.1 --ttl 1

Then point the client at 127.0.0.1:40443.

Only the Python standard library is used. No admin rights, no raw
sockets: the fake packet goes out on a normal TCP socket with IP_TTL set
(works on Linux/macOS/Windows).
"""
from __future__ import annotations

import argparse
import asyncio
import random
import socket
import sys
import time

METHODS = ("fragment", "fake_sni", "combined")
STRATEGIES = ("sni_split", "half", "multi", "tls_record_frag")
MULTI_CHUNK = 24
JITTER = 0.2

# --------------------------------------------------------------------------
# ClientHello logic — kept byte-for-byte in sync with the panel's engine
# (engines/engines/sni_spoofing.py); the unit tests assert both agree.
# --------------------------------------------------------------------------
def parse_client_hello(data: bytes) -> dict:
    try:
        if len(data) < 5 or data[0] != 0x16:
            return {}
        record_len = int.from_bytes(data[3:5], "big")
        if len(data) < 5 + record_len or data[5] != 0x01:
            return {}
        pos = 9
        pos += 2 + 32
        sid_len = data[pos]
        pos += 1 + sid_len
        cipher_len = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2 + cipher_len
        comp_len = data[pos]
        pos += 1 + comp_len
        ext_total = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2
        end = min(len(data), pos + ext_total)
        while pos + 4 <= end:
            ext_type = int.from_bytes(data[pos:pos + 2], "big")
            ext_len = int.from_bytes(data[pos + 2:pos + 4], "big")
            body = data[pos + 4: pos + 4 + ext_len]
            if ext_type == 0x0000 and len(body) >= 5:
                name_type = body[2]
                name_len = int.from_bytes(body[3:5], "big")
                name_start = pos + 4 + 5
                if name_type == 0 and 0 < name_len <= 255:
                    return {
                        "sni": body[5:5 + name_len].decode("ascii", "replace"),
                        "sni_start": name_start,
                        "sni_end": name_start + name_len,
                        "record_len": record_len,
                        "hello_len": len(data),
                    }
            pos += 4 + ext_len
    except (IndexError, ValueError):
        return {}
    return {}


def plan_fragments(data: bytes, strategy: str, rng: random.Random | None = None) -> list[bytes]:
    rng = rng or random.Random()
    if not data:
        return [data]
    strategy = (strategy or "sni_split").strip().lower()
    if strategy not in STRATEGIES:
        strategy = "sni_split"
    if strategy == "multi":
        size = max(8, MULTI_CHUNK + rng.randint(-6, 6))
        return [data[i:i + size] for i in range(0, len(data), size)] or [data]
    if strategy == "half":
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    if strategy == "tls_record_frag":
        if len(data) < 12:
            return [data]
        content = data[5:]
        hello = parse_client_hello(data)
        anchor = (hello.get("sni_start", 0) or 0) - 5
        cut = anchor if 0 < anchor < len(content) else len(content) // 2
        header = data[:5]
        return [
            header[:3] + len(content[:cut]).to_bytes(2, "big") + content[:cut],
            header[:3] + len(content[cut:]).to_bytes(2, "big") + content[cut:],
        ]
    hello = parse_client_hello(data)
    start, end = hello.get("sni_start", 0), hello.get("sni_end", 0)
    if not hello or end <= start + 1:
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    cut = start + (end - start) // 2
    return [data[:cut], data[cut:]]


def build_fake_client_hello(sni: str, rng: random.Random | None = None) -> bytes:
    rng = rng or random.Random()
    host = (sni or "").encode("ascii", "ignore")[:253] or b"www.microsoft.com"
    rand = bytes(rng.randrange(256) for _ in range(32))
    sid = bytes(rng.randrange(256) for _ in range(rng.choice((0, 8, 16, 32))))
    ciphers = bytes.fromhex("130113021303c02bc02fc02cc030009f009ecca9cca8")
    comp = b"\x01\x00"
    ext = (
        (0).to_bytes(2, "big")
        + (len(host) + 5).to_bytes(2, "big")
        + (len(host) + 3).to_bytes(2, "big")
        + b"\x00"
        + len(host).to_bytes(2, "big")
        + host
    )
    supported_groups = bytes.fromhex("001d00170018")
    ec_point = b"\x01\x02\x01\x00"
    ext += ((10).to_bytes(2, "big") + len(supported_groups).to_bytes(2, "big")
            + supported_groups)
    ext += ((11).to_bytes(2, "big") + len(ec_point).to_bytes(2, "big") + ec_point)
    body = (
        b"\x03\x03" + rand
        + len(sid).to_bytes(1, "big") + sid
        + len(ciphers).to_bytes(2, "big") + ciphers
        + comp
        + len(ext).to_bytes(2, "big") + ext
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def set_ttl(sock: socket.socket, ttl: int) -> None:
    """Low TTL on a NORMAL TCP socket — kernel honors IP_TTL; no raw socket,
    no admin rights needed."""
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, max(1, int(ttl)))


def delay_for(base_delay: float, rng: random.Random) -> float:
    return max(0.0, base_delay * (1.0 + rng.uniform(-JITTER, JITTER)))


# --------------------------------------------------------------------------
# The proxy itself
# --------------------------------------------------------------------------
class BypassProxy:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random()
        self.sem = asyncio.Semaphore(max(1, args.max_conns))

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        async with self.sem:
            try:
                await self._handle(reader, writer)
            except (ConnectionError, asyncio.TimeoutError, OSError):
                pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass
                log(f"closed {peer}")

    async def _handle(self, reader, writer) -> None:
        host, port = self.args.connect
        log(f"connection from client, dialing {host}:{port}")
        upstream = await asyncio.open_connection(host, port)
        # ---- intercept the ClientHello ------------------------------------
        hello = await self._read_client_hello(reader)
        method = self.args.method
        if hello and method in ("fake_sni", "combined"):
            await self._send_fake_packet(host, port)
        if hello and method in ("fragment", "combined"):
            parts = plan_fragments(hello, self.args.strategy, self.rng)
            log(f"clienthello {len(hello)}B sni={parse_client_hello(hello).get('sni')!r} "
                f"-> {len(parts)} fragments [{','.join(str(len(p)) for p in parts)}]")
            for i, part in enumerate(parts):
                upstream[1].write(part)
                await upstream[1].drain()
                if i < len(parts) - 1 and self.args.delay > 0:
                    await asyncio.sleep(delay_for(self.args.delay, self.rng))
        elif hello:
            upstream[1].write(hello)          # passthrough (safety net)
            await upstream[1].drain()
            log("method=fake_sni: real hello sent whole after the fake packet")
        # ---- bidirectional relay ------------------------------------------
        await asyncio.gather(
            self._pipe(reader, upstream[1]),
            self._pipe(upstream[0], writer),
            return_exceptions=True,
        )

    async def _read_client_hello(self, reader) -> bytes | None:
        """Read exactly one TLS record (the ClientHello) without losing bytes."""
        try:
            header = await asyncio.wait_for(reader.readexactly(5), timeout=15)
            if header[0] != 0x16:
                return header                     # not TLS: passthrough intact
            record_len = int.from_bytes(header[3:5], "big")
            if record_len <= 0 or record_len > 65535:
                return header
            body = await asyncio.wait_for(reader.readexactly(record_len), timeout=15)
            return header + body
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return None

    async def _send_fake_packet(self, host: str, port: int) -> None:
        """A synthetic 'allowed-SNI' ClientHello on a separate low-TTL
        connection: DPI classifies the flow as benign; the packet dies
        mid-path so the real server only ever sees the real hello."""
        sni = self.args.fake_sni
        if self.args.pool and self.args.rotate:
            sni = self.rng.choice(self.args.pool)
        fake = build_fake_client_hello(sni, self.rng)
        try:
            loop = asyncio.get_running_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            if self.args.ttl:
                set_ttl(sock, self.args.ttl)
            await loop.sock_connect(sock, (host, port))
            await loop.sock_sendall(sock, fake)
            await asyncio.sleep(delay_for(self.args.delay or 0.1, self.rng))
            sock.close()
            log(f"fake ClientHello sent (sni={sni}, ttl={self.args.ttl})")
        except OSError as exc:
            log(f"fake packet not delivered (fine): {exc}")

    async def _pipe(self, reader, writer) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass


def log(message: str) -> None:
    print(f"[emunel-sni] {time.strftime('%H:%M:%S')} {message}", flush=True)


def parse_connect(value: str) -> tuple[str, int]:
    host, _, port_s = (value or "").rpartition(":")
    if not host or not port_s.isdigit():
        raise SystemExit("--connect must be host:port (e.g. edge.example.com:443)")
    return host, int(port_s)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="EMUNEL SNI Bypass Helper (client side, stdlib only)")
    ap.add_argument("--listen", default="127.0.0.1:40443",
                    help="local listen address (default 127.0.0.1:40443)")
    ap.add_argument("--connect", required=True,
                    help="real server as host:port (your EMUNEL edge / TCP proxy)")
    ap.add_argument("--method", default="combined", choices=METHODS,
                    help="bypass method (default combined)")
    ap.add_argument("--strategy", default="sni_split", choices=STRATEGIES,
                    help="fragment strategy (default sni_split)")
    ap.add_argument("--delay", type=float, default=0.1,
                    help="inter-fragment delay seconds (default 0.1)")
    ap.add_argument("--ttl", type=int, default=1,
                    help="TTL for the fake packet (0 disables; default 1)")
    ap.add_argument("--fake-sni", default="www.microsoft.com",
                    help="SNI carried by the fake packet")
    ap.add_argument("--pool", default="",
                    help="comma-separated allowed-SNI pool to rotate through")
    ap.add_argument("--rotate", action="store_true",
                    help="rotate the fake SNI through the pool per connection")
    ap.add_argument("--max-conns", type=int, default=256,
                    help="concurrent connection bound (default 256)")
    args = ap.parse_args()

    lhost, _, lport = args.listen.rpartition(":")
    lhost, lport = (lhost or "127.0.0.1"), int(lport or 40443)
    args.connect = parse_connect(args.connect)
    args.pool = [s.strip() for s in args.pool.split(",") if s.strip()] if args.pool else []
    args.delay = min(2.0, max(0.0, args.delay))
    args.ttl = min(8, max(0, args.ttl))

    proxy = BypassProxy(args)
    log(f"listening on {lhost}:{lport} -> {args.connect[0]}:{args.connect[1]} "
        f"(method={args.method} strategy={args.strategy} delay={args.delay}s ttl={args.ttl})")
    log("point your client/browser at this local address")
    try:
        asyncio.run(_serve(proxy, lhost, lport))
    except KeyboardInterrupt:
        log("bye")


async def _serve(proxy: BypassProxy, host: str, port: int) -> None:
    server = await asyncio.start_server(proxy.handle, host, port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    main()
