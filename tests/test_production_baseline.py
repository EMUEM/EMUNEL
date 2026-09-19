import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from api.emunel_api.main import app
from core.emunel_core.app import ProxyServer
from core.emunel_core.config import CoreConfig
from core.emunel_core.quota import QuotaManager


def test_api_application_is_importable_and_has_health_route():
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert app.docs_url == "/api/docs"
    assert "/api/v1/network-tests/tcp" in paths


def test_protocol_detector_does_not_classify_invalid_hex_as_trojan():
    assert ProxyServer._detect_protocol(b"0f") == "trojan"
    assert ProxyServer._detect_protocol(b"g!") == "shadowsocks"
    assert ProxyServer._detect_protocol(b"\x00\x01") == "vless"


@pytest.mark.asyncio
async def test_unknown_credentials_are_denied_by_default():
    quota = QuotaManager(CoreConfig())
    result = await quota.check("not-registered")
    assert result.allowed is False
    assert result.reason == "Unknown credential."


@pytest.mark.asyncio
async def test_loaded_quota_is_enforced():
    quota = QuotaManager(CoreConfig())
    await quota.load_user_quota(
        "known", {"active": True, "traffic_limit_bytes": 10, "traffic_used_bytes": 9}
    )
    assert (await quota.check("known")).allowed is True
    await quota.consume("known", 1)
    assert (await quota.check("known")).allowed is False


@pytest.mark.asyncio
async def test_disabled_credentials_are_denied():
    quota = QuotaManager(CoreConfig())
    await quota.load_user_quota("disabled", {"active": False})
    result = await quota.check("disabled")
    assert result.allowed is False
    assert result.reason == "Credential disabled."
