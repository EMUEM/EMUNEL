"""Shared state for the consolidated engine modules — STABILIZATION stage 2.2.

Spec: "State مشترک (یک SQLite، یک Cache، یک Buffer Pool)" for the merged
modules. This module is exactly that trio:

  * SharedSQLite  — ONE WAL-mode SQLite file (consolidated.db in DATA_DIR)
                    shared by every merged module (Learning's Mesh +
                    Genetic tables live together in it; TrafficShaping and
                    Payload keep their tiny decision/journal rows there too)
  * DecisionCache — the merged TrafficShaping decision cache: TTL 300s,
                    max 100 entries (spec 3.2), LRU eviction, cleared by
                    the OOM guard under memory pressure
  * BufferPool    — bounded buffer accounting for streams: max 16 KiB per
                    stream (spec 3.3), total cap, simple get/put of bytes

All three are plain stdlib, thread-safe via a single lock, and bounded —
they can never grow the RSS without limit.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

DEFAULT_CACHE_TTL = 300.0     # spec 3.2: decision cache every 300s
DEFAULT_CACHE_MAX = 100       # spec 3.2: max 100 entries
DEFAULT_STREAM_BUFFER = 16 * 1024   # spec 3.3: max 16KB per stream
DEFAULT_POOL_TOTAL = 4 * 1024 * 1024  # 4MB global cap for pooled buffers


class SharedSQLite:
    """One SQLite connection shared by the merged modules.

    WAL journal + NORMAL sync (crash-safe on a Railway volume), busy
    timeout so concurrent writers wait instead of erroring, and a
    checkpoint helper the Learning cron calls to fold the -wal sidecar back
    into the main file (keeps the on-disk footprint small).
    """

    def __init__(self, data_dir: str | Path, *, filename: str = "consolidated.db"):
        self.path = Path(data_dir) / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False,
                                   timeout=3.0)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS shared_kv ("
            " module TEXT NOT NULL, key TEXT NOT NULL,"
            " value TEXT NOT NULL, updated_at REAL NOT NULL,"
            " PRIMARY KEY (module, key))")
        self.db.commit()
        self.lock = threading.Lock()

    # ---- tiny KV surface used by the merged modules -------------------------
    def set(self, module: str, key: str, value: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO shared_kv (module, key, value, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(module, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (module, key, value, time.time()))
            self.db.commit()

    def get(self, module: str, key: str, default: str = "") -> str:
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM shared_kv WHERE module=? AND key=?",
                (module, key)).fetchone()
        return row[0] if row else default

    def keys(self, module: str) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT key FROM shared_kv WHERE module=?", (module,)).fetchall()
        return [r[0] for r in rows]

    def checkpoint(self) -> bool:
        """Fold the WAL sidecar back into the main db (Learning cron)."""
        try:
            with self.lock:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return True
        except sqlite3.Error:
            return False

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            try:
                if side.exists():
                    total += side.stat().st_size
            except OSError:
                pass
        return total

    def close(self) -> None:
        try:
            with self.lock:
                self.db.close()
        except sqlite3.Error:
            pass


class DecisionCache:
    """TTL + LRU cache for pipeline decisions (spec 3.2).

    TrafficShaping uses it to skip recomputing the Morph/SNI decision for
    a repeat destination: cache hit = no bandit round-trip, no profile
    rebuild, no SNI pool scan — the measured win on the UI/response path.
    """

    def __init__(self, *, ttl: float = DEFAULT_CACHE_TTL,
                 max_entries: int = DEFAULT_CACHE_MAX):
        self.ttl = max(1.0, float(ttl))
        self.max_entries = max(4, int(max_entries))
        self._data: OrderedDict[str, tuple[object, float]] = OrderedDict()
        self.lock = threading.Lock()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get(self, key: str):
        with self.lock:
            hit = self._data.get(key)
            if hit is None:
                self.stats["misses"] += 1
                return None
            value, ts = hit
            if time.monotonic() - ts > self.ttl:
                del self._data[key]
                self.stats["misses"] += 1
                return None
            self._data.move_to_end(key)
            self.stats["hits"] += 1
            return value

    def put(self, key: str, value) -> None:
        with self.lock:
            self._data[key] = (value, time.monotonic())
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self.stats["evictions"] += 1

    def clear(self) -> int:
        with self.lock:
            n = len(self._data)
            self._data.clear()
        return n

    def __len__(self) -> int:
        with self.lock:
            return len(self._data)


class BufferPool:
    """Bounded byte-buffer accounting for the merged modules (spec 3.3).

    Streams may hold at most ``per_stream`` bytes (16 KiB default) of
    in-flight data; the pool refuses (returns False) when a stream exceeds
    it — callers then flush before buffering more, i.e. streaming instead
    of buffering. A global cap backstops the whole pool.
    """

    def __init__(self, *, per_stream: int = DEFAULT_STREAM_BUFFER,
                 total: int = DEFAULT_POOL_TOTAL):
        self.per_stream = max(1024, int(per_stream))
        self.total = max(self.per_stream, int(total))
        self.lock = threading.Lock()
        self._held: dict[str, int] = {}
        self.stats = {"granted": 0, "refused": 0, "peak_bytes": 0}

    def acquire(self, stream_id: str, nbytes: int) -> bool:
        nbytes = max(0, int(nbytes))
        with self.lock:
            held = self._held.get(stream_id, 0)
            if held + nbytes > self.per_stream:
                self.stats["refused"] += 1
                return False
            if sum(self._held.values()) + nbytes > self.total:
                # global pressure: refuse rather than grow
                self.stats["refused"] += 1
                return False
            self._held[stream_id] = held + nbytes
            self.stats["granted"] += 1
            self.stats["peak_bytes"] = max(
                self.stats["peak_bytes"], sum(self._held.values()))
            return True

    def release(self, stream_id: str, nbytes: int) -> None:
        with self.lock:
            held = self._held.get(stream_id, 0)
            left = held - max(0, int(nbytes))
            if left > 0:
                self._held[stream_id] = left
            else:
                self._held.pop(stream_id, None)

    def drain(self) -> int:
        """Hard-pressure hook (OOM guard): drop all accounting."""
        with self.lock:
            n = len(self._held)
            self._held.clear()
        return n

    def in_use(self) -> int:
        with self.lock:
            return sum(self._held.values())
