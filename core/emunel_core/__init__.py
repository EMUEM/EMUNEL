"""EMUNEL Core — the multi-protocol proxy runtime.

EMUNEL Core is the server runtime that EMUNEL instances run. It relays
VLESS / Trojan / Shadowsocks traffic over WebSocket and xHTTP transports,
exposes health/metrics APIs, and is managed remotely by the EMUNEL Console
through a EMUNEL Worker.

Derived from the RVG Gateway relay engine; refactored into a clean,
library-style package with explicit state boundaries.
"""
