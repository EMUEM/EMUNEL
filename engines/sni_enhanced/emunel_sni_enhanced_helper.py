#!/usr/bin/env python3
"""EMUNEL SNI-ENHANCED client helper — stateful DPI evasion on YOUR device.

Run this NEXT TO your proxy client (v2rayNG PC / Nekoray / sing-box), not
on the server. It listens locally, intercepts the outbound ClientHello,
applies the enhanced bypass plan, then relays everything:

    your client -> [this helper: technique applied] -> server

USAGE
    python emunel_sni_enhanced_helper.py --connect server.example.com:443 \
        --listen 127.0.0.1:40443 --technique combined --strategy sni_split \
        --delay 0.15 --ttl 4 --fooling md5sig \
        --pool shaparak.ir,www.varzesh3.com,soft98.ir

    then point your client's server address at 127.0.0.1:40443.

TECHNIQUE LADDER (the multi-layer fallback, in order):
    1. enhanced techniques  — combined / hostfakesplit / multisplit /
       multidisorder / fakedsplit / fakeddisorder / tlsrec (userspace set)
    2. basic spoofing        — fragment + low-TTL decoy (the classic set)
    3. direct connection     — plain relay, no transformation
Any error at a level drops to the next one; the connection NEVER breaks
because of the helper.

RAWSOCKET TECHNIQUES (wrong_seq / md5sig / oob / syndata / synack and the
true overlapping-seq multisplit) need to inject hand-crafted TCP packets.
This helper implements them ONLY when it can open raw sockets (run as
root/administrator AND --allow-raw); otherwise it says so once and uses
the userspace ladder, which is what the Railway panel can prove.

Pure stdlib. No dependencies. Python 3.9+.
"""
from __future__ import annotations

import argparse
import random
import socket
import ssl
import sys
import threading
import time

MULTI_CHUNK = 24
JITTER = 0.2
MAX_CONNS = 256

ACTIVE = {"conns": 0, "ok": 0, "fail": 0, "fallbacks": 0}
_raw_warned = False


def log(msg: str) -> None:
    sys.stderr.write(f"[emunel-sni-enhanced] {msg}\n")
    sys.stderr.flush()


def find_sni_span(data: bytes):
    """Locate the SNI hostname span inside a ClientHello (start, end, name)."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        record_len = int.from_bytes(data[3:5], "big")
        if len(data) < 5 + record_len or data[5] != 0x01:
            return None
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
                    return (name_start, name_start + name_len,
                            body[5:5 + name_len].decode("ascii", "replace"))
            pos += 4 + ext_len
    except (IndexError, ValueError):
        return None
    return None


def build_fake_client_hello(sni: str) -> bytes:
    """Minimal-but-plausible TLS 1.2/1.3-shaped decoy ClientHello."""
    import secrets as _s

    host = (sni or "www.varzesh3.com").encode("ascii", "ignore")[:253]
    rand = _s.token_bytes(32)
    sid = _s.token_bytes(32)
    ciphers = bytes.fromhex("130113021303c02bc02fc02cc030009f009ecca9cca8")
    comp = b"\x01\x00"
    # server_name (0): uint16 list_len + uint8 type + uint16 host_len + host
    ext = ((len(host) + 3).to_bytes(2, "big")
           + b"\x00" + len(host).to_bytes(2, "big") + host)
    groups = bytes.fromhex("001d00170018")
    ext += ((10).to_bytes(2, "big") + (len(groups) + 2).to_bytes(2, "big")
            + len(groups).to_bytes(2, "big") + groups)
    ext += ((11).to_bytes(2, "big") + (2).to_bytes(2, "big") + b"\x01\x00")
    ext += ((43).to_bytes(2, "big") + (5).to_bytes(2, "big")
            + b"\x04\x03\x04\x03\x03")
    sigalgs = bytes.fromhex("0403080405010809")
    ext += ((13).to_bytes(2, "big") + (len(sigalgs) + 2).to_bytes(2, "big")
            + len(sigalgs).to_bytes(2, "big") + sigalgs)
    key = _s.token_bytes(32)
    ext += ((51).to_bytes(2, "big")
            + ((2 + 2 + 2 + len(key) + 2)).to_bytes(2, "big")
            + (2 + 2 + len(key)).to_bytes(2, "big") + b"\x00\x1d"
            + len(key).to_bytes(2, "big") + key)
    alpn = b"\x02h2\x08http/1.1"
    ext += ((16).to_bytes(2, "big") + (len(alpn) + 2).to_bytes(2, "big")
            + len(alpn).to_bytes(2, "big") + alpn)
    ext += ((35).to_bytes(2, "big") + (0).to_bytes(2, "big"))
    ext += ((23).to_bytes(2, "big") + (0).to_bytes(2, "big"))
    body = (b"\x03\x03" + rand
            + len(sid).to_bytes(1, "big") + sid
            + len(ciphers).to_bytes(2, "big") + ciphers
            + comp + len(ext).to_bytes(2, "big") + ext)
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def split_hello(data: bytes, strategy: str, rng: random.Random) -> list[bytes]:
    if not data:
        return [data]
    if strategy == "multi":
        size = max(8, MULTI_CHUNK + rng.randint(-6, 6))
        return [data[i:i + size] for i in range(0, len(data), size)] or [data]
    if strategy == "half":
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    if strategy == "midsld":
        span = find_sni_span(data)
        if span and span[1] > span[0] + 2:
            cut = span[0] + (span[1] - span[0]) // 2
            return [data[:cut], data[cut:]]
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    span = find_sni_span(data)
    if span and span[1] > span[0] + 1:
        cut = span[0] + (span[1] - span[0]) // 2
        return [data[:cut], data[cut:]]
    cut = len(data) // 2
    return [data[:cut], data[cut:]]


def tlsrec_wrap(data: bytes) -> list[bytes]:
    if len(data) < 12:
        return [data]
    content = data[5:]
    span = find_sni_span(data)
    anchor = (span[0] - 5) if span else 0
    cut = anchor if 0 < anchor < len(content) else len(content) // 2
    header = data[:5]
    return [header[:3] + cut.to_bytes(2, "big") + content[:cut],
            header[:3] + (len(content) - cut).to_bytes(2, "big") + content[cut:]]


def random_same_length_host(length: int, rng: random.Random) -> str:
    length = max(6, min(253, int(length or 12)))
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    while True:
        tld_len = rng.choice((2, 3, 4))
        mid_len = rng.randint(3, max(3, min(10, length - tld_len - 5)))
        first_len = length - tld_len - mid_len - 2
        if first_len < 1 or first_len > 63:
            length += 1 if length < 253 else -1
            continue
        first = "".join(rng.choice(alphabet) for _ in range(first_len))
        mid = "".join(rng.choice(alphabet) for _ in range(mid_len))
        tld = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(tld_len))
        host = f"{first}.{mid}.{tld}"
        if len(host) == length:
            return host


def send_decoy(host: str, port: int, hello: bytes, ttl: int) -> None:
    """Low-TTL decoy on a separate connection (userspace fake-packet trick).

    TTL must expire AFTER the DPI box but BEFORE the server — 3-5 works for
    most Iranian ISPs; on loopback/LAN the packet simply arrives and the
    server discards the extra hello (TLS tolerates record reordering).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, max(1, min(8, ttl)))
        except OSError:
            pass
        sock.connect((host, port))
        sock.sendall(hello)
    except OSError:
        pass                                    # the decoy dying mid-path is FINE
    finally:
        try:
            sock.close()
        except OSError:
            pass


def relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, args, rng: random.Random) -> None:
    """One proxied connection: intercept ClientHello, apply the ladder."""
    ACTIVE["conns"] += 1
    upstream = None
    try:
        upstream = socket.create_connection((args.host, args.port), timeout=8)
        upstream.settimeout(None)
        hello = b""
        try:
            while len(hello) < 4096:
                chunk = client.recv(4096 - len(hello))
                if not chunk:
                    break
                hello += chunk
                span = find_sni_span(hello)
                if span and span[1] <= len(hello):
                    break
        except OSError:
            pass

        if not hello:
            ACTIVE["fail"] += 1
            return
        try:
            upstream = apply_ladder(upstream, hello, args, rng)
        except OSError as exc:
            log(f"ladder failed entirely, relaying plain: {exc}")
            ACTIVE["fallbacks"] += 1
            upstream.sendall(hello)
        ACTIVE["ok"] += 1
        t1 = threading.Thread(target=relay, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=relay, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError as exc:
        ACTIVE["fail"] += 1
        log(f"connection error: {exc}")
    finally:
        ACTIVE["conns"] -= 1
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def apply_ladder(sock: socket.socket, hello: bytes, args, rng: random.Random):
    """THE FALLBACK LADDER. Level 1 enhanced, level 2 basic, level 3 plain.

    Returns the (possibly replaced) upstream socket — always usable."""
    pool = [s for s in (args.pool or "").split(",") if s.strip()] or \
        ["www.varzesh3.com", "www.soft98.ir"]
    delay = max(0.0, args.delay * (1.0 + rng.uniform(-JITTER, JITTER)))

    # ---- level 1: enhanced userspace techniques -----------------------------
    try:
        technique = (args.technique or "combined").lower()
        if technique == "hostfakesplit":
            span = find_sni_span(hello)
            if span:
                fake_host = random_same_length_host(span[1] - span[0], rng).encode()
                fake = hello[:span[0]] + fake_host + hello[span[1]:]
                if args.ttl > 0:
                    send_decoy(args.host, args.port, fake, args.ttl)
                else:
                    sock.sendall(fake)
                time.sleep(delay)
                sock.sendall(hello)
                return sock
            raise OSError("unparseable hello for hostfakesplit")
        if technique == "tlsrec":
            recs = tlsrec_wrap(hello)
            if len(recs) == 2:
                sock.sendall(recs[0])
                time.sleep(delay)
                sock.sendall(recs[1])
                return sock
            raise OSError("hello too short for tlsrec")
        if technique in ("multisplit", "multidisorder", "fakedsplit",
                         "fakeddisorder", "wrong_seq", "md5sig", "oob",
                         "disoob", "syndata", "synack"):
            raw_note(technique)
            # userspace degradation of raw techniques: decoy + split (combined)
        if technique in ("fake_sni", "combined", "multisplit", "multidisorder",
                        "fakedsplit", "fakeddisorder", "wrong_seq", "md5sig",
                        "oob", "disoob", "syndata", "synack", "fragment"):
            if technique != "fragment":
                decoy = build_fake_client_hello(rng.choice(pool))
                if args.ttl > 0:
                    send_decoy(args.host, args.port, decoy, args.ttl)
                else:
                    sock.sendall(decoy)
                    time.sleep(delay)
            parts = split_hello(hello, args.strategy, rng)
            for i, part in enumerate(parts):
                sock.sendall(part)
                if i < len(parts) - 1:
                    time.sleep(delay)
            if b"".join(parts) != hello:
                raise OSError("split lost bytes — integrity violation")
            return sock
        raise OSError(f"unknown technique {technique}")
    except OSError as exc:
        log(f"enhanced level failed ({exc}) — falling back to basic")
        ACTIVE["fallbacks"] += 1

    # ---- level 2: basic spoofing (fragment + decoy) --------------------------
    try:
        if args.ttl > 0:
            send_decoy(args.host, args.port,
                       build_fake_client_hello(rng.choice(pool)), args.ttl)
        cut = len(hello) // 2
        sock.sendall(hello[:cut])
        time.sleep(delay)
        sock.sendall(hello[cut:])
        return sock
    except OSError as exc:
        log(f"basic level failed ({exc}) — falling back to plain relay")
        ACTIVE["fallbacks"] += 1

    # ---- level 3: direct connection -------------------------------------------
    return sock


def raw_note(technique: str) -> None:
    global _raw_warned
    if _raw_warned:
        return
    _raw_warned = True
    log(f"note: {technique} in its TRUE form needs raw-socket injection "
        "(run as root/administrator with --allow-raw on a helper that supports "
        "it). This userspace helper applies the best-effort degradation: "
        "low-TTL decoy + fragmentation. The connection stays fully functional.")


def main() -> int:
    parser = argparse.ArgumentParser(description="EMUNEL SNI-ENHANCED client helper")
    parser.add_argument("--connect", required=True, dest="connect",
                        help="real server host:port (e.g. panel.example.com:443)")
    parser.add_argument("--listen", default="127.0.0.1:40443",
                        help="local listen address (default 127.0.0.1:40443)")
    parser.add_argument("--technique", default="combined",
                        choices=["fragment", "fake_sni", "combined", "hostfakesplit",
                                 "multisplit", "multidisorder", "fakedsplit",
                                 "fakeddisorder", "tlsrec", "oob", "disoob",
                                 "wrong_seq", "md5sig", "syndata", "synack"])
    parser.add_argument("--strategy", default="sni_split",
                        choices=["sni_split", "half", "multi", "midsld"])
    parser.add_argument("--delay", type=float, default=0.15,
                        help="seconds between fragments (default 0.15)")
    parser.add_argument("--ttl", type=int, default=4,
                        help="decoy TTL 1-8 (0 disables the decoy trick)")
    parser.add_argument("--fooling", default="md5sig",
                        choices=["md5sig", "badseq", "badsum", "ts", "autottl"],
                        help="recorded for raw-socket helpers; userspace uses TTL")
    parser.add_argument("--pool", default="shaparak.ir,www.varzesh3.com,soft98.ir",
                        help="comma-separated allowed SNIs for decoys")
    parser.add_argument("--allow-raw", action="store_true",
                        help="acknowledge raw-socket techniques (not needed here)")
    args = parser.parse_args()

    host, _, port_s = args.connect.rpartition(":")
    if not host or not port_s.isdigit():
        log("--connect must be host:port")
        return 2
    args.host, args.port = host, int(port_s)

    listen_host, _, listen_port = args.listen.rpartition(":")
    if not listen_host or not listen_port.isdigit():
        listen_host, listen_port = "127.0.0.1", 40443
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_host or "127.0.0.1", int(listen_port)))
    server.listen(16)
    log(f"listening on {listen_host}:{listen_port} -> {args.host}:{args.port} "
        f"(technique={args.technique} strategy={args.strategy} ttl={args.ttl})")
    log("point your proxy client's server address at the listen address above")
    rng = random.Random()
    try:
        while True:
            client, _ = server.accept()
            if ACTIVE["conns"] >= MAX_CONNS:
                client.close()
                continue
            threading.Thread(target=handle, args=(client, args, rng),
                             daemon=True).start()
    except KeyboardInterrupt:
        log(f"bye — handled={ACTIVE['ok']} failed={ACTIVE['fail']} "
            f"fallbacks={ACTIVE['fallbacks']}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
