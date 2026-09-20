"""Engine registry — every engine class, wired to the manager by name.

Names here are what EMUNEL_PIPELINE_ORDER refers to. Host affinity:
  * "console" — runs in the Console process (gateway hop + configgen)
  * "core"    — runs inside the Core process through engines.core_host
"""
from __future__ import annotations

from .coalescing import CoalescingEngine
from .morphing import MorphingEngine
from .compression import CompressionEngine
from .preconnect import PreConnectEngine
from .fec import FECEngine
from .congestion import CongestionEngine
from .session_resumption import SessionResumptionEngine
from .fake_handshake import FakeHandshakeEngine
from .split_tunneling import SplitTunnelingEngine
from .sni_rotation import SNIRotationEngine
from .domain_fronting import DomainFrontingEngine
from .port_hopping import PortHoppingEngine

REGISTRY: dict[str, type] = {
    "Coalesce": CoalescingEngine,
    "Morph": MorphingEngine,
    "Compress": CompressionEngine,
    "PreConnect": PreConnectEngine,
    "FEC": FECEngine,
    "Congestion": CongestionEngine,
    "SessionResumption": SessionResumptionEngine,
    "FakeHandshake": FakeHandshakeEngine,
    "SplitTunnel": SplitTunnelingEngine,
    "SNIRotation": SNIRotationEngine,
    "DomainFronting": DomainFrontingEngine,
    "PortHopping": PortHoppingEngine,
}

__all__ = ["REGISTRY"]
