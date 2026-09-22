"""Consolidated engine modules — STABILIZATION stage 2.

Four merged groups wrap the legacy engines (legacy code preserved, just
wrapped — see facade.py). Feature flags (all default false):

    TRAFFIC_SHAPING_MERGED   -> TrafficShaping (Morph + SNIEnhanced + Chaos)
    TRANSPORT_MERGED         -> Transport (PreConnect + Congestion + FEC +
                                 SessionResumption)
    LEARNING_MERGED          -> Learning (Mesh + Genetic + Synergy)
    PAYLOAD_MERGED           -> Payload (Coalesce + Compress)

LEGACY_ENGINES_ENABLED=true (default) keeps the standalone engines as the
fallback path: when a merged module fails to start, the manager
automatically falls back to running that group's engines individually.
"""
from .facade import ConsolidatedEngine
from .learning import LearningEngine
from .payload import PayloadEngine
from .shared import BufferPool, DecisionCache, SharedSQLite
from .traffic_shaping import TrafficShapingEngine
from .transport import TransportEngine

# merged module NAME -> (class, group id, its legacy children)
MERGED_MODULES: dict[str, dict] = {
    "TrafficShaping": {
        "cls": TrafficShapingEngine,
        "group": "traffic_shaping",
        "children": ("Morph", "SNIEnhanced", "Chaos"),
        "flag": "traffic_shaping_merged",
        "pipeline_var": "EMUNEL_TS_PIPELINE",
    },
    "Transport": {
        "cls": TransportEngine,
        "group": "transport",
        "children": ("FEC", "PreConnect", "SessionResumption", "Congestion"),
        "flag": "transport_merged",
        "pipeline_var": "EMUNEL_TRANSPORT_PIPELINE",
    },
    "Learning": {
        "cls": LearningEngine,
        "group": "learning",
        "children": ("Mesh", "Genetic", "Synergy"),
        "flag": "learning_merged",
        "pipeline_var": "EMUNEL_LEARNING_PIPELINE",
    },
    "Payload": {
        "cls": PayloadEngine,
        "group": "payload",
        "children": ("Compress", "Coalesce"),
        "flag": "payload_merged",
        "pipeline_var": "EMUNEL_PAYLOAD_PIPELINE",
    },
}

# flat child NAME -> merged module NAME
CHILD_TO_GROUP: dict[str, str] = {
    child: name
    for name, spec in MERGED_MODULES.items()
    for child in spec["children"]
}

__all__ = [
    "ConsolidatedEngine", "TrafficShapingEngine", "TransportEngine",
    "LearningEngine", "PayloadEngine", "SharedSQLite", "DecisionCache",
    "BufferPool", "MERGED_MODULES", "CHILD_TO_GROUP",
]
