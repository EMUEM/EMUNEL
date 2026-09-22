"""Rotating state backups — STABILIZATION stage 1.3.

Why: on Railway WITHOUT a volume mounted at /data every redeploy wipes the
container filesystem, and WITH a volume a crashed process can still leave
state behind. The console database (emunel.db) + the engine state store
(state.json) are what make instances, users and engine toggles survive.
This service copies them, on a cron, into ``DATA_DIR/backups/``:

  * JSON state files  — plain copy (the state store already writes
                        atomically via tmp+rename, so a copy is never
                        half-written)
  * SQLite databases  — copied through sqlite3's backup API, which produces
                        a consistent snapshot even while the source
                        connection is live (WAL included)
  * rotation          — timestamped dirs ``backup-YYYYmmdd-HHMMSS``; only
                        the newest EMUNEL_BACKUP_KEEP (default 7) survive
  * volume guard      — when the backup area exceeds EMUNEL_BACKUP_MAX_MB
                        (default 64) the oldest copies are pruned first;
                        if pruning cannot free enough space the newest
                        copy is still written and the oldest dropped

Scope (allowlist, deliberately small so backups stay fast and tiny):
  DATA_DIR/state.json                engine toggles + metrics
  DATA_DIR/*.json                   other engine-layer state (mesh/genetic
                                     keep their state in their own DBs, but
                                     any JSON the layer drops here is small)
  DATA_DIR/*.db                      mesh.db, genetic.db, consolidated.db
  DATA_DIR/../emunel.db              the console database — but ONLY when it
                                     is a real SQLite file living next to
                                     the engines dir on the same volume
                                     (a PostgreSQL deployment simply has no
                                     such file and is skipped)

Never-crash contract: every step is individually wrapped; a failure is
logged once and retried on the next cron tick. The service runs inside the
console host only (cores are per-instance and their state is disposable).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path


def ensure_wal(db_path: str | Path, *, timeout: float = 2.0) -> bool:
    """Flip a SQLite file to WAL journal mode from the outside.

    The journal mode is a persistent property of the file, so this works
    even for databases owned by other processes (the console's aiosqlite
    handle). A live writer can briefly block the switch — that is fine,
    the caller retries on the next tick. Returns True when the file is in
    WAL mode after the call."""
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=timeout)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            return bool(mode) and str(mode[0]).lower() == "wal"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


class BackupService:
    """Cron-driven rotating backup of the engines-layer + console state."""

    def __init__(self, data_dir: str | Path, *,
                 interval_h: float = 6.0,
                 keep: int = 7,
                 max_mb: float = 64.0):
        self.data_dir = Path(data_dir)
        self.backups_dir = self.data_dir / "backups"
        self.interval_h = max(0.25, float(interval_h))
        self.keep = max(1, int(keep))
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.last_run: float | None = None
        self.last_error: str | None = None
        self.stats = {"backups": 0, "files": 0, "pruned": 0, "errors": 0,
                      "wal_applied": 0}
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # first run shortly after boot (a fresh deploy should carry a backup
        # ASAP), then on the regular cadence
        self._task = asyncio.create_task(self._loop(first_delay=90.0))

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self, *, first_delay: float = 0.0) -> None:
        delay = first_delay
        while not self._stopping:
            try:
                await asyncio.sleep(delay)
                if self._stopping:
                    return
                await asyncio.to_thread(self.run_once)
                delay = max(0.25, self.interval_h) * 3600.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # never kill the host
                self.stats["errors"] += 1
                self.last_error = f"{exc}"
                delay = 300.0           # retry in 5 minutes

    # ---- the actual work (sync, run in a worker thread) ----------------------
    def run_once(self, *, reason: str = "cron") -> dict:
        started = time.time()
        out = {"ok": True, "reason": reason, "files": 0, "pruned": 0,
               "bytes": 0, "error": ""}
        try:
            self._wal_sweep()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = self.backups_dir / f"backup-{stamp}"
            # same-second collision (rapid/manual runs): unique suffix so no
            # run ever overwrites another; suffixes still sort AFTER the base
            n = 0
            while dest.exists():
                n += 1
                dest = self.backups_dir / f"backup-{stamp}-{n:02d}"
            dest.mkdir(parents=True, exist_ok=True)
            copied = 0
            total = 0
            for src in self._sources():
                try:
                    size = self._copy_one(src, dest / src.name)
                except Exception as exc:
                    self.stats["errors"] += 1
                    self.last_error = f"{src.name}: {exc}"
                    continue
                copied += 1
                total += size
            manifest = {
                "created_at": time.time(),
                "reason": reason,
                "files": copied,
                "bytes": total,
                "sources": [s.name for s in self._sources()],
            }
            (dest / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8")
            out["files"] = copied
            out["bytes"] = total
            out["pruned"] = self._rotate()
            self.stats["backups"] += 1
            self.stats["files"] += copied
            self.stats["pruned"] += out["pruned"]
            self.last_run = time.time()
        except Exception as exc:
            self.stats["errors"] += 1
            self.last_error = f"{exc}"
            out = {"ok": False, "reason": reason, "error": str(exc)}
        out["took_ms"] = round((time.time() - started) * 1000, 1)
        return out

    # ---- helpers -------------------------------------------------------------
    def _sources(self) -> list[Path]:
        """Allowlist of files worth backing up (small + critical)."""
        out: list[Path] = []
        if self.data_dir.is_dir():
            for p in sorted(self.data_dir.iterdir()):
                if not p.is_file():
                    continue
                if p.suffix == ".json" and p.name != "manifest.json":
                    out.append(p)
                elif p.suffix == ".db":
                    out.append(p)
        # the console DB lives one level up on the same volume
        # (/data/emunel.db next to /data/engines) — but only when it is a
        # real SQLite file (PostgreSQL deployments have none)
        parent_db = self.data_dir.parent / "emunel.db"
        if parent_db.is_file():
            out.append(parent_db)
        return out

    def _wal_sweep(self) -> None:
        """Ensure every *.db in scope runs in WAL mode (idempotent)."""
        for src in self._sources():
            if src.suffix == ".db" and ensure_wal(src):
                self.stats["wal_applied"] += 1

    def _copy_one(self, src: Path, dest: Path) -> int:
        if src.suffix == ".db":
            # consistent snapshot through the sqlite3 backup API
            src_conn = sqlite3.connect(str(src), timeout=3.0)
            try:
                dst_conn = sqlite3.connect(str(dest), timeout=3.0)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        else:
            shutil.copy2(src, dest)
        return dest.stat().st_size if dest.exists() else 0

    def _rotate(self) -> int:
        """Keep only the newest `keep` backups; enforce the size cap."""
        pruned = 0
        try:
            entries = sorted(
                (p for p in self.backups_dir.iterdir()
                 if p.is_dir() and p.name.startswith("backup-")),
                key=lambda p: p.name)          # timestamped names sort by time
            # 1) count rotation
            while len(entries) > self.keep:
                oldest = entries.pop(0)
                shutil.rmtree(oldest, ignore_errors=True)
                pruned += 1
            # 2) size cap (defensive: each copy is tiny, but a huge state
            #    file must never fill the volume)
            while self._tree_size(self.backups_dir) > self.max_bytes \
                    and len(entries) > 1:
                oldest = entries.pop(0)
                shutil.rmtree(oldest, ignore_errors=True)
                pruned += 1
        except OSError:
            pass
        if pruned:
            self.stats["pruned"] = self.stats.get("pruned", 0)
        return pruned

    @staticmethod
    def _tree_size(root: Path) -> int:
        total = 0
        try:
            for p in root.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def status(self) -> dict:
        return {
            "enabled": self._task is not None and not self._task.done(),
            "interval_h": self.interval_h,
            "keep": self.keep,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "stats": dict(self.stats),
            "backups_dir": str(self.backups_dir),
        }
