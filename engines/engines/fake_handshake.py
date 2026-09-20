"""Fake Handshake Response Engine — active-probing defense.

Probes (GFW-style scanners, masscan-style sweeps) hit random paths on the
public port. Before this engine, every unknown path answered with the
EMUNEL panel HTML — a clear proxy-panel fingerprint. Now, requests that do
not look like a browser (no Mozilla UA + text/html Accept pair) receive a
byte-exact default page of a stock web server instead.

Browser users are never affected: they send both signals, and the panel
only loads from them anyway.
"""
from __future__ import annotations

from ..base import KIND_CONFIGGEN, Engine, EngineContext

_NGINX_PAGE = b"""<html>
<head><title>404 Not Found</title></head>
<body>
<center><h1>404 Not Found</h1></center>
<hr><center>nginx</center>
</body>
</html>
"""

_APACHE_PAGE = b"""<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
<html><head>
<title>404 Not Found</title>
</head><body>
<h1>Not Found</h1>
<p>The requested URL was not found on this server.</p>
<hr>
<address>Apache Server at localhost Port 80</address>
</body></html>
"""

_NGINX_HEADERS = [
    (b"server", b"nginx/1.24.0 (Ubuntu)"),
    (b"content-type", b"text/html"),
]
_APACHE_HEADERS = [
    (b"server", b"Apache/2.4.57 (Unix)"),
    (b"content-type", b"text/html; charset=iso-8859-1"),
]


def fake_page(cfg) -> bytes:
    if cfg.fake_server_type == "apache":
        return _APACHE_PAGE
    return _NGINX_PAGE


def fake_headers(cfg) -> tuple[int, list[tuple[bytes, bytes]]]:
    if cfg.fake_server_type == "apache":
        return 404, list(_APACHE_HEADERS)
    return 404, list(_NGINX_HEADERS)


class FakeHandshakeEngine(Engine):
    NAME = "FakeHandshake"
    TITLE = "Fake Handshake — probe requests see a stock nginx/apache, not the panel"
    HANDLES = frozenset({KIND_CONFIGGEN})
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:
        self.status.metrics.update({"probes_answered": 0, "browser_passthroughs": 0})

    async def process(self, ctx: EngineContext) -> EngineContext:
        # The actual interception lives in engines/middleware._FakePageInterceptor
        # (it must run at the ASGI layer to replace responses). This engine
        # provides the page/headers and keeps the status/metrics contract.
        return ctx

    def record_probe(self) -> None:
        self.status.metrics["probes_answered"] += 1

    def record_browser(self) -> None:
        self.status.metrics["browser_passthroughs"] += 1

    def defaults(self) -> dict:
        return {"EMUNEL_FAKE_SERVER_TYPE": self.cfg.fake_server_type}
