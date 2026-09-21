"""Synergy package — coordination between the three evolution engines."""
from .engine import SynergyEngine
from .peers import get, register, snapshot, unregister
from .synergy_manager import SynergyManager

__all__ = ["SynergyEngine", "SynergyManager", "peers",
           "register", "unregister", "get", "snapshot"]
