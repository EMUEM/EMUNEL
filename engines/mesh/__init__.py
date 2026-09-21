"""DpiMesh engine package (additive, flag-gated off by default)."""
from .aggregator import build_policies, group_rows
from .mesh_engine import MeshEngine, privacy_hash

__all__ = ["MeshEngine", "build_policies", "group_rows", "privacy_hash"]
