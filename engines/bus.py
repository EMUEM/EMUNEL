"""Engine Event Bus — the only channel between engines and the host.

Guarantees:
  * publishing NEVER blocks the data path — subscribers get bounded queues
    and a full queue drops the event (counted) instead of applying
    backpressure to a relay pumping frames
  * one slow or broken subscriber cannot affect others
  * everything is fire-and-forget; engines must treat events as hints

Topics used by the platform:
  * engine.lifecycle   — {name, action: start|stop|bypass|disable|enable}
  * engine.error      — {name, error, where}
  * feedback.conn      — {engine: "Morph", isp, profile, ok, bytes, duration}
  * probe.result       — {engine, ok, detail}
  * metrics.tick       — periodic registry snapshot
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    topic: str
    payload: dict
    ts: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {"topic": self.topic, "ts": self.ts, "payload": self.payload}


class _Subscription:
    __slots__ = ("queue", "dropped")

    def __init__(self, maxsize: int):
        self.queue: deque[Event] = deque(maxlen=maxsize)
        self.dropped = 0


class EventBus:
    """Minimal bounded fan-out bus. Safe to call from async code only
    (publish is synchronous + non-blocking; drains run in the background)."""

    def __init__(self, queue_size: int = 256):
        self._subs: dict[str, list[_Subscription]] = {}
        self._wildcards: list[_Subscription] = []
        self._queue_size = queue_size
        self._dropped_total = 0
        self._published_total = 0
        self._log: deque[Event] = deque(maxlen=200)

    # ---- publish / subscribe ------------------------------------------------
    def publish(self, topic: str, payload: dict) -> None:
        """Non-blocking; drops for full subscribers."""
        event = Event(topic=topic, payload=payload)
        self._published_total += 1
        self._log.append(event)
        targets = list(self._subs.get(topic, ()))
        targets.extend(self._wildcards)
        for sub in targets:
            if len(sub.queue) == sub.queue.maxlen:
                sub.dropped += 1
                self._dropped_total += 1
                continue
            sub.queue.append(event)

    def subscribe(self, topic: str = "*") -> "_Subscription":
        """topic="*" subscribes to everything."""
        sub = _Subscription(self._queue_size)
        if topic == "*":
            self._wildcards.append(sub)
        else:
            self._subs.setdefault(topic, []).append(sub)
        return sub

    def unsubscribe(self, sub: "_Subscription") -> None:
        for holders in (self._wildcards, *self._subs.values()):
            if sub in holders:
                holders.remove(sub)
                return

    # ---- convenience for engines ---------------------------------------------
    async def next_event(self, sub: "_Subscription", timeout: float | None = None):
        """Wait for one event from a subscription (or None on timeout).

        Engines that need to react to events run a loop around this; the
        timeout lets them check their own stop condition.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if sub.queue:
                return sub.queue.popleft()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(remaining, 0.05))
            else:
                await asyncio.sleep(0.05)

    # ---- introspection --------------------------------------------------------
    def stats(self) -> dict:
        return {
            "published": self._published_total,
            "dropped": self._dropped_total,
            "topics": {t: len(s) for t, s in self._subs.items()},
            "recent": [e.to_dict() for e in list(self._log)[-25:]],
        }
