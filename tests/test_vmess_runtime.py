"""VMess runtime contracts. Live Xray test is opt-in and loopback-only."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from emunel_core.config import CoreConfig
from emunel_core.state import Link
from emunel_core.links import generate_share_link
from emunel_core.relay.vmess import XrayRuntime, RuntimeUnavailable, verify_binary

UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_missing_binary_clear_error():
    with pytest.raises(RuntimeUnavailable, match="EMUNEL_XRAY_BINARY"):
        verify_binary(CoreConfig())


def test_binary_requires_sha256(tmp_path):
    binary = tmp_path / "xray"
    binary.write_bytes(b"not an executable to run")
    binary.chmod(0o700)
    cfg = CoreConfig(xray_binary=str(binary))
    with pytest.raises(RuntimeUnavailable, match="SHA256"):
        verify_binary(cfg)
    cfg.xray_sha256 = "0" * 64
    with pytest.raises(RuntimeUnavailable, match="mismatch"):
        verify_binary(cfg)
    cfg.xray_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert verify_binary(cfg) == str(binary)


def test_vmess_real_client_share_contract():
    link = Link(uuid=UUID, label="test", protocol="vmess-ws")
    uri = generate_share_link(link, "proxy.example", path_prefix="/i/token")
    assert uri.startswith("vmess://")
    data = json.loads(base64.b64decode(uri.removeprefix("vmess://")))
    assert data["id"] == UUID
    assert data["aid"] == "0"
    assert data["net"] == "ws"
    assert data["scy"] == "auto"
    assert data["path"] == f"/i/token/vmess-ws/{UUID}"
    assert data["tls"] == "tls"
    assert data["port"] == "443"


def test_xray_config_is_single_user_loopback_aead():
    config = XrayRuntime.config(UUID, 12345)
    inbound = config["inbounds"][0]
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["protocol"] == "vmess"
    assert inbound["settings"]["clients"] == [{"id": UUID, "alterId": 0}]
    assert inbound["settings"]["disableInsecureEncryption"] is True
    assert inbound["streamSettings"]["wsSettings"]["path"] == f"/vmess-ws/{UUID}"
