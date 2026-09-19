"""EMUNEL Core — Subscription link generation."""

import base64
import json
import urllib.parse
from typing import Optional

from .config import CoreConfig


class LinkManager:
    """Generate subscription links for various protocols."""

    def __init__(self, config: CoreConfig) -> None:
        self.config = config

    def generate_vless_link(
        self,
        uuid: str,
        address: str,
        port: int = 443,
        remark: str = "EMUNEL",
        network: str = "ws",
        security: str = "tls",
        sni: Optional[str] = None,
        path: str = "/",
    ) -> str:
        """Generate a VLESS subscription link."""
        params = {
            "type": network,
            "security": security,
            "path": path,
        }
        if sni:
            params["sni"] = sni

        query = urllib.parse.urlencode(params)
        link = f"vless://{uuid}@{address}:{port}?{query}#{urllib.parse.quote(remark)}"
        return link

    def generate_trojan_link(
        self,
        password: str,
        address: str,
        port: int = 443,
        remark: str = "EMUNEL",
        network: str = "tcp",
        security: str = "tls",
        sni: Optional[str] = None,
    ) -> str:
        """Generate a Trojan subscription link."""
        params = {
            "type": network,
            "security": security,
        }
        if sni:
            params["sni"] = sni

        query = urllib.parse.urlencode(params)
        link = f"trojan://{password}@{address}:{port}?{query}#{urllib.parse.quote(remark)}"
        return link

    def generate_ss_link(
        self,
        method: str,
        password: str,
        address: str,
        port: int = 443,
        remark: str = "EMUNEL",
    ) -> str:
        """Generate a Shadowsocks subscription link."""
        user_info = base64.urlsafe_b64encode(
            f"{method}:{password}".encode()
        ).decode().rstrip("=")
        link = f"ss://{user_info}@{address}:{port}#{urllib.parse.quote(remark)}"
        return link

    def generate_subscription(
        self, links: list[str], base64_encode: bool = True
    ) -> str:
        """Generate a subscription response with all links."""
        content = "\n".join(links)
        if base64_encode:
            return base64.b64encode(content.encode()).decode()
        return content
