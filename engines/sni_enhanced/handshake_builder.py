"""Fake ClientHello builder with uTLS-style fingerprints.

/handshake_builder — builds decoy ClientHello bytes for the enhanced SNI
engine. Three fingerprint shapes are modelled (chrome / firefox / safari)
through cipher-suite ordering, extension ordering and a TLS 1.3
supported_versions extension — close enough to survive a DPI parser that
fingerprint-hello lengths, cipher lists and extension order.

Also provides hostfakesplit's core primitive: replacing the SNI inside a
REAL ClientHello with a random hostname of EXACTLY the same length, so the
byte layout (record lengths, extension lengths, offsets) stays identical —
the DPI's first look sees a benign name, the server still receives the
real one afterwards.

Pure stdlib; deterministic under a provided rng (tests rely on that).
"""
from __future__ import annotations

import random
import secrets

# Fingerprint shapes: cipher suites in browser-observed order + extension
# order. These are decoys, not security artifacts.
FINGERPRINTS = ("chrome", "firefox", "safari", "randomized")

_FP_CIPHERS = {
    # TLS 1.3 suites first (as modern browsers send them), then the
    # ECDHE-GCM/AES suites Chrome/firefox negotiate.
    "chrome": "130113021303c02bc02fc02cc030009f009ecca9cca8",
    "firefox": "130113021303c02bc02fc02cc030009f009e",
    "safari": "130113021303c02bc02fc02cc030009c009d",
}
_FP_ALPN = {
    "chrome": b"\x02h2\x08http/1.1",
    "firefox": b"\x02h2\x08http/1.1",
    "safari": b"\x02h2\x08http/1.1",
}
_FP_EXT_ORDER = {
    # extension type ids in the order the browser sends them (decoy-realistic)
    "chrome": (0, 23, 65281, 10, 11, 35, 16, 5, 34, 51, 43, 13, 45, 28),
    "firefox": (0, 23, 65281, 10, 11, 35, 16, 5, 34, 51, 43, 13),
    "safari": (0, 65281, 10, 11, 35, 16, 5, 13),
}


def _fingerprint(name: str) -> str:
    name = (name or "chrome").strip().lower()
    if name == "randomized":
        name = secrets.choice(("chrome", "firefox", "safari"))
    return name if name in _FP_CIPHERS else "chrome"


def build_fake_client_hello(sni: str, fingerprint: str = "chrome",
                            rng: random.Random | None = None) -> bytes:
    """A structurally valid TLS 1.2/1.3-shaped ClientHello carrying ``sni``.

    Only needs to survive a DPI parser long enough to be classified as
    ordinary browser TLS to an allowed site. Session id and random are
    randomized; extension order follows the fingerprint profile.
    """
    rng = rng or random.Random()
    fp = _fingerprint(fingerprint)
    host = (sni or "").encode("ascii", "ignore")[:253] or b"www.varzesh3.com"
    rand = secrets.token_bytes(32)
    sid = secrets.token_bytes(rng.choice((32, 32, 0)))
    ciphers = bytes.fromhex(_FP_CIPHERS[fp])
    comp = b"\x01\x00"

    exts: dict[int, bytes] = {}
    # server_name (0): uint16 list_len + uint8 type + uint16 host_len + host
    exts[0] = ((len(host) + 3).to_bytes(2, "big")
               + b"\x00" + len(host).to_bytes(2, "big") + host)
    # supported_groups (10): x25519, secp256r1, secp384r1
    groups = bytes.fromhex("001d00170018")
    exts[10] = len(groups).to_bytes(2, "big") + groups
    # ec_point_formats (11): list_len=1, uncompressed
    exts[11] = b"\x01\x00"
    # session_ticket (35): empty
    exts[35] = b""
    # encrypt_then_mac (22) / extended_master_secret (23)
    exts[22] = b""
    exts[23] = b""
    # renegotiation_info (65281)
    exts[65281] = b"\x00"
    # signature_algorithms (13)
    sigalgs = bytes.fromhex("0403080405010809060104030503")
    exts[13] = len(sigalgs).to_bytes(2, "big") + sigalgs
    # supported_versions (43): list_len=4, TLS 1.3 + TLS 1.2
    exts[43] = b"\x04\x03\x04\x03\x03"
    # psk_key_exchange_modes (45): psk_dhe_ke
    exts[45] = b"\x01\x01"
    # key_share (51): uint16 shares_len + uint16 group(x25519) + uint16 key_len + key
    key = secrets.token_bytes(32)
    exts[51] = ((2 + 2 + len(key)).to_bytes(2, "big") + b"\x00\x1d"
                + len(key).to_bytes(2, "big") + key)
    # ALPN (16)
    alpn = _FP_ALPN[fp]
    exts[16] = len(alpn).to_bytes(2, "big") + alpn
    # padding (21): pad the hello to a browser-plausible size band
    body_target = rng.randint(480, 560)
    pad = b"\x00" * 0

    ext_bytes = b""
    for ext_type in _FP_EXT_ORDER[fp]:
        if ext_type not in exts:
            continue
        payload = exts[ext_type]
        ext_bytes += ext_type.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload
    if not pad:
        pass
    # record-level padding extension (21) to land in the size band
    current = 10 + 32 + 1 + len(sid) + 2 + len(ciphers) + len(comp) + 2 + len(ext_bytes)
    need = body_target - current - 4
    if need > 0:
        ext_bytes += (21).to_bytes(2, "big") + need.to_bytes(2, "big") + b"\x00" * need

    body = (b"\x03\x03" + rand
            + len(sid).to_bytes(1, "big") + sid
            + len(ciphers).to_bytes(2, "big") + ciphers
            + comp
            + len(ext_bytes).to_bytes(2, "big") + ext_bytes)
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def find_sni_span(data: bytes) -> tuple[int, int, str] | None:
    """Locate the SNI hostname span inside a ClientHello.

    Returns (start, end_exclusive, hostname) or None. Byte-exact re-use of
    the proven parser from engines.engines.sni_spoofing (kept local so the
    package stays self-contained for the helper generator).
    """
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


def random_same_length_host(length: int, rng: random.Random | None = None) -> str:
    """A plausible hostname of EXACTLY ``length`` chars (hostfakesplit).

    Shape: label.label.tld with lowercase alnum — same total length as the
    hostname it replaces, so every length field in the hello still parses.
    """
    rng = rng or random.Random()
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
        tld = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz")
                      for _ in range(tld_len))
        host = f"{first}.{mid}.{tld}"
        if len(host) == length:
            return host


def build_hostfakesplit_hello(real_hello: bytes,
                              rng: random.Random | None = None) -> bytes | None:
    """Replace the real SNI in-place with a same-length random hostname.

    Record lengths, extension lengths and offsets are untouched because
    the replacement is byte-for-byte the same length — the DPI's first SNI
    read sees the fake name; the real hello follows afterwards and the
    server processes that one. Returns None when the hello is unparseable
    (caller sends the original untouched).
    """
    rng = rng or random.Random()
    span = find_sni_span(real_hello)
    if not span:
        return None
    start, end, _ = span
    fake_host = random_same_length_host(end - start, rng).encode("ascii")
    if len(fake_host) != end - start:      # paranoia: never shift bytes
        return None
    return real_hello[:start] + fake_host + real_hello[end:]
