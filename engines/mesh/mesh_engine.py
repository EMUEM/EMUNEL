"""DpiMesh engine — crowd-sourced DPI map, privacy-preserving by design.

Panel users share DPI signatures; the engine builds a live map of which
protocol shapes survive which ISP/region. Storage is plain SQLite (stdlib
`sqlite3`) on the engine data dir — the Railway volume mount point
(/data/engines in production), honouring the "SQLite on the volume, no
external database" platform rule.

Differential privacy (operator spec, enforced here):
  * isp_hash    = SHA256(ISP    + ":" + MESH_SALT)[:12]
  * region_hash = SHA256(Region + ":" + MESH_SALT)[:16]
  * No IP address and no user ID is ever stored.
  * The salt lives in env (MESH_SALT) — default "change_me_please".

Resource guards (free-tier budget):
  * In-engine rate limiting per isp hash (default 30/min) and globally
    (600/min) — a hostile client cannot inflate the map or the CPU.
  * Retention: signatures older than MESH_RETENTION_HOURS (default 24h)
    are pruned on every aggregation sweep.
  * Volume guard: mesh.db over MESH_MAX_DB_MB (default 10MB) flips the
    engine into DEGRADED mode — reports are answered with a fallback
    marker instead of being stored, policies keep serving from the last
    known-good table, and the Core is never involved.

With DPI_MESH_ENABLED=false the engine never starts: no DB is opened and
no data is collected anywhere.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from ..base import Engine
from .aggregator import VALID_RESULTS, build_policies

SCHEMA = """
CREATE TABLE IF NOT EXISTS dpi_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isp_hash TEXT NOT NULL,
    region_hash TEXT NOT NULL,
    time_bucket INTEGER NOT NULL,
    protocol TEXT NOT NULL DEFAULT '',
    transport TEXT NOT NULL DEFAULT '',
    sni TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    latency REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sig_key
    ON dpi_signatures(isp_hash, region_hash, time_bucket);
CREATE INDEX IF NOT EXISTS idx_sig_created
    ON dpi_signatures(created_at);
CREATE TABLE IF NOT EXISTS dpi_policies (
    isp_hash TEXT NOT NULL,
    region_hash TEXT NOT NULL,
    recommended_protocol TEXT NOT NULL DEFAULT '',
    recommended_params TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (isp_hash, region_hash)
);
"""

BUCKET_SEC = 300          # 5-minute time buckets (directive pipeline)
MAX_FIELD = 64            # every incoming string field is capped
MAX_REPORT_BYTES = 1024   # reports are ~200B; anything past 1KB is junk


def privacy_hash(value: str, salt: str, length: int) -> str:
    return hashlib.sha256(f"{(value or '')[:MAX_FIELD]}:{salt}".encode("utf-8")
                          ).hexdigest()[:length]


class _RateLimiter:
    """Token bucket per key + one global bucket (userspace, O(1))."""

    def __init__(self, per_key_per_min: int = 30, global_per_min: int = 600):
        self.per_key = max(1, per_key_per_min)
        self.per_global = max(1, global_per_min)
        self._keys: dict[str, tuple[float, float]] = {}
        self._global: tuple[float, float] = (time.monotonic(), float(self.per_global))

    def _bucket(self, key: str) -> tuple[float, float]:
        now = time.monotonic()
        state = self._keys.get(key)
        if state is None:
            state = (now, float(self.per_key))
            self._keys[key] = state
            if len(self._keys) > 4096:               # memory bound
                cutoff = now - 120.0
                self._keys = {k: v for k, v in self._keys.items()
                              if v[0] > cutoff}
        return state

    @staticmethod
    def _fill(state: tuple[float, float], rate: float) -> tuple[float, float]:
        now = time.monotonic()
        tokens = min(float(rate), state[1] + (now - state[0]) * (rate / 60.0))
        return now, tokens

    def allow(self, key: str) -> bool:
        now, tokens = self._fill(self._bucket(key), float(self.per_key))
        self._keys[key] = (now, tokens)
        gnow, gtokens = self._fill(self._global, float(self.per_global))
        self._global = (gnow, gtokens)
        if tokens < 1.0 or gtokens < 1.0:
            return False
        self._keys[key] = (now, tokens - 1.0)
        self._global = (gnow, gtokens - 1.0)
        return True


class MeshEngine(Engine):
    NAME = "Mesh"
    TITLE = "DpiMesh — crowd-sourced DPI map (privacy-preserving)"
    HANDLES = frozenset()
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002 (interface)
        self.db: sqlite3.Connection | None = None
        self.degraded = False
        self.degraded_reason = ""
        self._limiter = _RateLimiter()
        self._sub = None
        self._listener_task: asyncio.Task | None = None
        self._agg_task: asyncio.Task | None = None
        self.status.metrics.update({
            "reports": 0, "stored": 0, "rejected": 0, "rate_limited": 0,
            "policies": 0, "aggregations": 0, "degraded_events": 0,
            "pruned": 0,
        })

    async def start(self) -> None:
        from ..synergy import peers

        try:
            path = Path(self.cfg.data_dir) / "mesh.db"
            self.db = sqlite3.connect(str(path), check_same_thread=False,
                                      timeout=2.0)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            self.db.commit()
        except sqlite3.Error as exc:
            # storage broken: degrade instead of failing the host
            self._enter_degraded(f"sqlite open failed: {exc}")
        peers.register("Mesh", self)
        # standalone wiring: chaos self-play outcomes enrich the map
        self._sub = self.bus.subscribe("chaos.outcome")
        self._listener_task = asyncio.create_task(self._outcome_listener())
        self._agg_task = asyncio.create_task(self._aggregator_loop())
        self.log.info("DpiMesh up (aggregate every %ss)"
                      % self.cfg.mesh_aggregate_interval_sec)

    async def stop(self) -> None:
        for task in (self._listener_task, self._agg_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._listener_task = None
        self._agg_task = None
        if self._sub is not None:
            with contextlib.suppress(Exception):
                self.bus.unsubscribe(self._sub)
            self._sub = None
        with contextlib.suppress(Exception):
            from ..synergy import peers
            peers.unregister("Mesh", self)
        if self.db is not None:
            with contextlib.suppress(sqlite3.Error):
                self.db.close()
            self.db = None

    # ---- ingest -------------------------------------------------------------
    def report(self, *, isp: str, region: str, protocol: str, transport: str,
               sni: str, result: str, latency: float | None) -> dict:
        """Store one signature. Never raises — the answer says what happened
        (the client treats it as fire-and-forget)."""
        try:
            if self.db is None or self.degraded:
                self.status.metrics["rejected"] = \
                    int(self.status.metrics.get("rejected", 0)) + 1
                return {"stored": False, "fallback": True,
                        "reason": self.degraded_reason or "engine stopped"}
            if self._volume_exceeded():
                self._enter_degraded("volume cap reached (MESH_MAX_DB_MB)")
                return {"stored": False, "fallback": True,
                        "reason": self.degraded_reason}
            isp_hash = privacy_hash(str(isp), self.cfg.mesh_salt, 12)
            if not self._limiter.allow(isp_hash):
                self.status.metrics["rate_limited"] = \
                    int(self.status.metrics.get("rate_limited", 0)) + 1
                return {"stored": False, "fallback": False,
                        "reason": "rate limited"}
            clean_result = str(result or "").strip().lower()[:MAX_FIELD]
            if clean_result not in VALID_RESULTS:
                clean_result = "fail" if clean_result else "ok"
            try:
                lat = None if latency is None else float(latency)
            except (TypeError, ValueError):
                lat = None
            if lat is not None:
                lat = max(0.0, min(lat, 300_000.0))
            now = time.time()
            self.db.execute(
                "INSERT INTO dpi_signatures (isp_hash, region_hash, "
                "time_bucket, protocol, transport, sni, result, latency, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (isp_hash,
                 privacy_hash(str(region), self.cfg.mesh_salt, 16),
                 int(now // BUCKET_SEC),
                 str(protocol or "")[:MAX_FIELD],
                 str(transport or "")[:MAX_FIELD],
                 str(sni or "")[:MAX_FIELD],
                 clean_result, lat, now))
            self.db.commit()
            self.status.metrics["reports"] = \
                int(self.status.metrics.get("reports", 0)) + 1
            self.status.metrics["stored"] = \
                int(self.status.metrics.get("stored", 0)) + 1
            # privacy: only hashes ever leave the engine
            self.bus.publish("mesh.report", {"isp_hash": isp_hash})
            return {"stored": True}
        except sqlite3.Error as exc:
            self._enter_degraded(f"sqlite write failed: {exc}")
            return {"stored": False, "fallback": True,
                    "reason": self.degraded_reason}

    async def _outcome_listener(self) -> None:
        """chaos.outcome -> signature rows (the synergy feedback cycle,
        also active standalone)."""
        while True:
            try:
                event = await self.bus.next_event(self._sub, timeout=5.0)
                if event is None or event.topic != "chaos.outcome":
                    continue
                payload = event.payload or {}
                self.report(
                    isp="chaos-lab", region="loopback",
                    protocol="chaos",
                    transport="+".join(payload.get("frames") or [])[:MAX_FIELD]
                             or "chaos",
                    sni="", result="ok" if payload.get("ok") else "fail",
                    latency=payload.get("latency_ms"))
            except asyncio.CancelledError:
                raise
            except Exception:                          # noqa: BLE001
                await asyncio.sleep(1.0)

    # ---- aggregation -----------------------------------------------------------
    async def _aggregator_loop(self) -> None:
        while True:
            await asyncio.sleep(max(5.0, float(self.cfg.mesh_aggregate_interval_sec)))
            try:
                self.aggregate()
            except Exception as exc:                    # noqa: BLE001
                self.log.error(f"aggregation failed: {exc}")

    def aggregate(self) -> int:
        """Sweep: derive policies from the last hour, prune old rows.
        Returns the number of live policies (0 when degraded)."""
        if self.degraded or self.db is None:
            return 0
        if self._volume_exceeded():
            self._enter_degraded("volume cap reached (MESH_MAX_DB_MB)")
            return 0
        cutoff = time.time() - 3600.0
        rows = self.db.execute(
            "SELECT isp_hash, region_hash, transport, protocol, result, "
            "latency FROM dpi_signatures WHERE created_at > ?",
            (cutoff,)).fetchall()
        policies = build_policies(rows)
        now = time.time()
        for p in policies:
            self.db.execute(
                "INSERT INTO dpi_policies (isp_hash, region_hash, "
                "recommended_protocol, recommended_params, confidence, "
                "updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(isp_hash, region_hash) DO UPDATE SET "
                "recommended_protocol=excluded.recommended_protocol, "
                "recommended_params=excluded.recommended_params, "
                "confidence=excluded.confidence, updated_at=excluded.updated_at",
                (p["isp_hash"], p["region_hash"], p["recommended_protocol"],
                 json.dumps(p["recommended_params"]), p["confidence"], now))
        # retention: keep the signature table bounded
        prune_before = time.time() - max(1, self.cfg.mesh_retention_hours) * 3600
        cur = self.db.execute(
            "DELETE FROM dpi_signatures WHERE created_at < ?", (prune_before,))
        pruned = cur.rowcount or 0
        self.db.commit()
        self.status.metrics["policies"] = len(policies)
        self.status.metrics["aggregations"] = \
            int(self.status.metrics.get("aggregations", 0)) + 1
        if pruned:
            self.status.metrics["pruned"] = \
                int(self.status.metrics.get("pruned", 0)) + pruned
        if policies:
            self.bus.publish("mesh.policy", {
                "count": len(policies),
                "keys": [f'{p["isp_hash"][:8]}/{p["region_hash"][:8]}'
                         for p in policies[:20]],
            })
        return len(policies)

    # ---- queries -----------------------------------------------------------------
    def policy(self, isp: str, region: str) -> dict:
        isp_hash = privacy_hash(str(isp), self.cfg.mesh_salt, 12)
        region_hash = privacy_hash(str(region), self.cfg.mesh_salt, 16)
        if self.db is None:
            return {"isp_hash": isp_hash, "region_hash": region_hash,
                    "policy": None, "enabled": False}
        row = self.db.execute(
            "SELECT recommended_protocol, recommended_params, confidence, "
            "updated_at FROM dpi_policies WHERE isp_hash=? AND region_hash=?",
            (isp_hash, region_hash)).fetchone()
        if row is None:
            return {"isp_hash": isp_hash, "region_hash": region_hash,
                    "policy": None, "reason": "no data yet — reports build "
                    "policies after the next aggregation"}
        try:
            params = json.loads(row[1] or "{}")
        except ValueError:
            params = {}
        return {
            "isp_hash": isp_hash, "region_hash": region_hash,
            "policy": {
                "recommended_protocol": row[0],
                "params": params,
                "confidence": row[2],
                "updated_at": row[3],
            },
        }

    def policies(self, limit: int = 50) -> list[dict]:
        """The DPI map (for the Evolution tab) — hashed keys only."""
        if self.db is None:
            return []
        rows = self.db.execute(
            "SELECT isp_hash, region_hash, recommended_protocol, "
            "recommended_params, confidence, updated_at FROM dpi_policies "
            "ORDER BY confidence DESC LIMIT ?",
            (max(1, min(int(limit), 200)),)).fetchall()
        out = []
        for isp_hash, region_hash, proto, params, confidence, updated in rows:
            try:
                parsed = json.loads(params or "{}")
            except ValueError:
                parsed = {}
            out.append({
                "isp_hash": isp_hash, "region_hash": region_hash,
                "recommended_protocol": proto, "params": parsed,
                "confidence": confidence, "updated_at": updated,
            })
        return out

    def transport_stats(self, hours: float = 1.0) -> dict:
        """Per-transport aggregates — GeneticEngine's fitness input."""
        if self.db is None:
            return {}
        cutoff = time.time() - max(0.1, hours) * 3600.0
        rows = self.db.execute(
            "SELECT transport, result, latency FROM dpi_signatures "
            "WHERE created_at > ?", (cutoff,)).fetchall()
        agg: dict[str, dict] = {}
        for transport, result, latency in rows:
            key = (str(transport) or "unknown")[:32]
            bucket = agg.setdefault(key, {"ok": 0, "total": 0, "lat": []})
            bucket["total"] += 1
            if str(result).lower() == "ok":
                bucket["ok"] += 1
            if latency is not None:
                bucket["lat"].append(float(latency))
        out = {}
        for key, bucket in agg.items():
            avg = sum(bucket["lat"]) / len(bucket["lat"]) if bucket["lat"] else 0.0
            var = (sum((x - avg) ** 2 for x in bucket["lat"])
                   / len(bucket["lat"])) if bucket["lat"] else 0.0
            out[key] = {
                "success_rate": round(bucket["ok"] / bucket["total"], 4)
                if bucket["total"] else 0.0,
                "avg_latency_ms": round(avg, 2),
                "jitter_ms": round(var ** 0.5, 2),
                "samples": bucket["total"],
            }
        return out

    # ---- volume guard --------------------------------------------------------------
    def _volume_exceeded(self) -> bool:
        if self.db is None:
            return True
        try:
            base = Path(self.cfg.data_dir) / "mesh.db"
            if not base.exists():
                return False
            # WAL mode: recent writes live in the -wal sidecar — count the
            # whole footprint, not just the main file
            total = base.stat().st_size
            for suffix in ("-wal", "-shm"):
                side = base.with_name(base.name + suffix)
                if side.exists():
                    total += side.stat().st_size
            return total > self.cfg.mesh_max_db_mb * 1024 * 1024
        except OSError:
            return False

    def _enter_degraded(self, reason: str) -> None:
        if self.degraded:
            return
        self.degraded = True
        self.degraded_reason = reason
        self.status.metrics["degraded_events"] = \
            int(self.status.metrics.get("degraded_events", 0)) + 1
        self.log.warning(
            f"DEGRADED ({reason}) — reports fall back to no-op, policies "
            f"serve last-known-good; the Core is unaffected")

    def defaults(self) -> dict:
        return {
            "aggregate_interval_sec": self.cfg.mesh_aggregate_interval_sec,
            "retention_hours": self.cfg.mesh_retention_hours,
            "max_db_mb": self.cfg.mesh_max_db_mb,
            "salt_configured": self.cfg.mesh_salt != "change_me_please",
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }
