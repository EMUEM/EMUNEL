"""SNI Enhanced package — stateful DPI evasion control plane (isolated).

Everything lives in engines/sni_enhanced/ per the operator's structure
spec; only three ADDITIVE wiring points exist outside this folder:
engines/engines/__init__.py (registry), engines/config.py (flags, default
false) and engines/api.py (the /api/engines/sni/enhanced/* routes).
"""
from __future__ import annotations

from .enhanced_engine import SNIEnhancedEngine
from .injection import TECHNIQUES, FOOLING
from .sni_pool import SNIPool, load_default_snis, load_strategies

__all__ = [
    "SNIEnhancedEngine",
    "TECHNIQUES",
    "FOOLING",
    "SNIPool",
    "load_default_snis",
    "load_strategies",
]
