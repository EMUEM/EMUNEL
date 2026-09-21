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
from .sni_spoofing import SNISpoofingEngine
from .reality import RealityEngine
# Evolution engines (additive packages — flag-gated OFF by default)
from ..chaos import ChaosEngine
from ..mesh import MeshEngine
from ..genetic import GeneticEngine
from ..synergy import SynergyEngine
# SNI Enhanced (stateful DPI evasion — flag-gated OFF by default)
from ..sni_enhanced import SNIEnhancedEngine

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
    "SNISpoof": SNISpoofingEngine,
    "Reality": RealityEngine,
    # Evolution engines — CHAOS_PROTOCOL_ENABLED / DPI_MESH_ENABLED /
    # GENETIC_ENGINE_ENABLED / SYNERGY_ENABLED (all default false)
    "Chaos": ChaosEngine,
    "Mesh": MeshEngine,
    "Genetic": GeneticEngine,
    "Synergy": SynergyEngine,
    # SNI Enhanced — SNI_ENHANCED_ENABLED (default false)
    "SNIEnhanced": SNIEnhancedEngine,
}

__all__ = ["REGISTRY"]
