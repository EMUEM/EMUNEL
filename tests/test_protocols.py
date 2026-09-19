"""EMUNEL Core protocol unit tests — ported from Lunel's wire-level suite.

VLESS/Trojan header codecs, SS AEAD streams, quota policy, share links.
These tests speak the protocols as real clients would.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import emunel_core.relay.vless as vless_mod
from emunel_core.relay.trojan import (
    build_trojan_request,
    parse_trojan_request,
    trojan_password_hash,
)
from emunel_core.relay.vless import build_vless_header, parse_vless_header
from emunel_core.state import Link


def test_vless_header_roundtrip_domain():
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    header = build_vless_header(uuid, "example.com", 443)
    command, address, port, rest = parse_vless_header(header + b"payload")
    assert command == 1
    assert address == "example.com"
    assert port == 443
    assert rest == b"payload"


def test_vless_header_roundtrip_ipv4():
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    header = build_vless_header(uuid, "1.2.3.4", 8080)
    command, address, port, rest = parse_vless_header(header)
    assert address == "1.2.3.4"
    assert port == 8080
    assert rest == b""


def test_vless_header_roundtrip_ipv6():
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    header = build_vless_header(uuid, "2001:db8::1", 443)
    command, address, port, rest = parse_vless_header(header)
    # parser returns the fully expanded IPv6 form (reference behavior)
    assert address == "2001:0db8:0000:0000:0000:0000:0000:0001"
    assert port == 443


def test_trojan_header_roundtrip():
    password = "5f8c1a2b-9de0-4f77-8c21-aabbccddeeff"
    request = build_trojan_request(password, "example.org", 993, b"hello")
    address, port, payload = parse_trojan_request(request, trojan_password_hash(password))
    assert address == "example.org"
    assert port == 993
    assert payload == b"hello"


def test_trojan_wrong_password_rejected():
    from contextlib import suppress

    good = trojan_password_hash("right")
    request = build_trojan_request("wrong", "example.org", 993)
    raised = False
    with suppress(PermissionError):
        try:
            parse_trojan_request(request, good)
        except PermissionError:
            raised = True
    assert raised


def test_ss_aead_roundtrip():
    from emunel_core.relay.shadowsocks import AEADStream, derive_key

    cipher = "chacha20-ietf-poly1305"
    master = derive_key("hunter2-secret", 32)
    enc = AEADStream(master, cipher)
    enc.set_master_key(master)
    dec = AEADStream(master, cipher)
    dec.set_master_key(master)

    wire = b""
    for chunk in (b"first frame", b"second", b"third " * 50):
        wire += enc.encrypt_chunk(chunk)
    dec.feed(wire)
    out = list(dec.try_decrypt_chunks())
    assert out[0] == b"first frame"
    assert b"".join(out[1:]) == b"second" + b"third " * 50


def test_ss_aead_wrong_key_rejected():
    from emunel_core.relay.shadowsocks import AEADStream, derive_key

    cipher = "aes-256-gcm"
    enc = AEADStream(derive_key("right-password", 32), cipher)
    enc.set_master_key(derive_key("right-password", 32))
    dec = AEADStream(derive_key("wrong-password", 32), cipher)
    dec.set_master_key(derive_key("wrong-password", 32))
    dec.feed(enc.encrypt_chunk(b"secret"))
    try:
        list(dec.try_decrypt_chunks())
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_link_quota_policy():
    link = Link(uuid="a" * 32, label="t", protocol="vless-ws", limit_bytes=100, used_bytes=95)
    assert link.is_allowed() is True
    link.used_bytes = 100
    assert link.is_allowed() is False
    link.limit_bytes = 0  # unlimited
    assert link.is_allowed() is True


def test_link_expiry_policy():
    from datetime import datetime, timedelta, timezone

    link = Link(
        uuid="b" * 32, label="t", protocol="trojan-ws",
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    assert link.is_expired() is True
    assert link.is_allowed() is False  # expired link must not relay


def test_link_alpn_strips_h2():
    link = Link(uuid="c" * 32, label="t", protocol="vless-ws", alpn="h2,http/1.1")
    assert link.alpn == "http/1.1"  # WS upgrades fail over h2


def test_share_links_render_for_every_protocol():
    from emunel_core.links import generate_share_link

    uuid = "123e4567-e89b-12d3-a456-426614174000"
    cases = {
        "vless-ws": "vless://",
        "trojan-ws": "trojan://",
        "xhttp-packet-up": "vless://",
        "xhttp-stream-up": "vless://",
        "trojan-xhttp-packet-up": "trojan://",
        "trojan-xhttp-stream-up": "trojan://",
        "shadowsocks": "ss://",
    }
    for proto, scheme in cases.items():
        link = Link(uuid=uuid, label="test", protocol=proto,
                    ss_cipher="chacha20-ietf-poly1305", ss_password="pw123")
        url = generate_share_link(link, "proxy.example.com")
        assert url.startswith(scheme), f"{proto} -> {url[:30]}"
        assert "proxy.example.com" in url


def test_share_link_path_prefix_is_applied():
    from emunel_core.links import generate_share_link

    uuid = "123e4567-e89b-12d3-a456-426614174000"
    link = Link(uuid=uuid, label="t", protocol="vless-ws")
    url = generate_share_link(link, "proxy.example.com", path_prefix="/i/tok123")
    assert "%2Fi%2Ftok123" in url or "/i/tok123" in url
