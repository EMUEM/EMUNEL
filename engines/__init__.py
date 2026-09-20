"""EMUNEL Engines — a plugin layer for the existing platform.

Design contract (per the operator's spec):

  * The proxy Core (core/emunel_core) is NEVER modified. Engines attach
    from the outside:
      - console side: an ASGI wrapper installed by main.py around the
        Console app (frame pipeline on the /i/* gateway hop, config
        rewrites on subscription feeds, probe defense on fallback pages)
      - core side:    ``python -m engines.core_host`` wraps the Core ASGI
        app (launched that way by the worker when core-side engines are
        active) and hooks its outbound dials — again without touching a
        single Core file.
  * Every engine implements the same lifecycle:
        init(config) -> start() -> process(ctx) -> feedback(metrics) -> stop()
  * Engines receive work through the EngineManager pipeline (order from
    env: EMUNEL_PIPELINE_ORDER / PIPELINE_ORDER) and talk to each other /
    to the host only through the EventBus.
  * An engine that raises is automatically bypassed for the current batch
    and, after N consecutive failures, disabled for a cooldown — the
    platform keeps running exactly as before (graceful degradation).
  * All tunables come from environment variables; defaults live in
    engines/config.py and are documented in .env.example.
  * State and logs persist under the engine data dir (default /data/engines,
    i.e. the Railway volume mount point). A probe file detects non-persistent
    data dirs and warns the operator to attach a Railway volume.
"""
from .config import EngineEnv, env
from .bus import EventBus, Event

__all__ = ["EngineEnv", "env", "EventBus", "Event"]
