"""Peer registry — how the evolution engines find each other.

The EngineManager intentionally keeps engines isolated (they only receive
cfg + bus + state), so the evolution engines register themselves here on
start() and unregister on stop(). A plain dict of strong references is
deliberate: the registry must never resurrect a stopped engine, and the
manager guarantees stop() runs on teardown (crash = process death, which
clears the registry with it).
"""
from __future__ import annotations

_REGISTRY: dict[str, object] = {}


def register(name: str, engine: object) -> None:
    _REGISTRY[str(name)] = engine


def unregister(name: str, engine: object) -> None:
    # only drop OUR instance (a restarted engine may already be in place)
    if _REGISTRY.get(str(name)) is engine:
        _REGISTRY.pop(str(name), None)


def get(name: str):
    return _REGISTRY.get(str(name))


def snapshot() -> dict:
    return dict(_REGISTRY)
