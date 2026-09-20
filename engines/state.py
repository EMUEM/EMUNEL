"""Engine state persistence + the Railway volume probe.

State (bandit arms, chosen profiles, metrics, engine logs) lives in the
engine data dir — by default /data/engines, i.e. the Railway volume mount
point. Writes are atomic (tmp + rename) and debounced exactly like the
Core's StateStore, so a crash never leaves a half-written file.

Volume probe: on boot we look for the probe file written by the previous
run. A surviving token means the directory persisted across the restart —
no warning. On Railway WITHOUT a volume mounted at /data the filesystem
is ephemeral, so a *first* boot there (no probe) means engine state will
reset on every redeploy — the manager logs a loud warning telling the
operator to attach a Railway volume at /data (this is *storage*
persistence, and is completely separate from user traffic quotas).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

PROBE_FILE = ".engine-volume-probe"
STATE_FILE = "state.json"
LOG_DIR = "logs"


class EngineStateStore:
    """Per-process engine state store (one per host: console/core)."""

    def __init__(self, data_dir: str, *, flush_interval: float = 5.0):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / LOG_DIR).mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / STATE_FILE
        self._flush_interval = flush_interval
        self._lock = threading.Lock()
        self._data: dict = {"version": 1, "saved_at": 0.0, "engines": {}}
        self._dirty = False
        self._last_flush = 0.0
        self.volume_warning: str | None = self._volume_probe()
        self._logs: dict[str, deque[str]] = {}

    # ---- volume probe --------------------------------------------------------
    def _volume_probe(self) -> str | None:
        """Detect non-persistent engine storage. Any probe token left by a
        previous run proves the directory survived the restart — the token's
        VALUE is irrelevant (it changes every boot by design), only its
        presence matters. A missing probe means a first boot on this storage;
        on Railway without a /data volume that storage is ephemeral, which
        deserves the volume-attach advisory. Everywhere else: silent."""
        probe = self.dir / PROBE_FILE
        token = secrets.token_urlsafe(16)
        try:
            previous = probe.read_text(encoding="utf-8").strip() if probe.exists() else ""
        except OSError:
            previous = ""
        try:
            probe.write_text(token, encoding="utf-8")
        except OSError:
            pass
        if previous:
            # A probe written by a previous run is still here: the directory
            # persisted across the restart. No warning.
            return None
        on_railway = any(k.startswith("RAILWAY_") for k in os.environ)
        on_volume = str(self.dir).startswith("/data")
        if on_railway and not on_volume:
            return (
                "engine data at "
                f"{self.dir} sits on the container's ephemeral filesystem — engine "
                "state resets on every redeploy. Attach a Railway volume "
                "mounted at /data (Settings -> Volumes) so engine state, "
                "learned ISP profiles and logs survive restarts. This is "
                "storage persistence only and is unrelated to user traffic "
                "quotas."
            )
        return None

    # ---- engine payloads -------------------------------------------------------
    def get(self, engine_name: str, default: dict | None = None) -> dict:
        with self._lock:
            engines = self._data.setdefault("engines", {})
            payload = engines.get(engine_name)
            if payload is None:
                payload = dict(default or {})
                engines[engine_name] = payload
            return payload

    def set(self, engine_name: str, payload: dict) -> None:
        with self._lock:
            self._data.setdefault("engines", {})[engine_name] = payload
            self._dirty = True

    def mutate(self, engine_name: str, fn) -> None:
        with self._lock:
            engines = self._data.setdefault("engines", {})
            fn(engines.get(engine_name, {}))
            engines[engine_name] = engines.get(engine_name, {})
            self._dirty = True

    # ---- load / save ----------------------------------------------------------
    def load(self) -> None:
        try:
            if self.state_path.exists():
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("engines"), dict):
                    with self._lock:
                        self._data = raw
                    return
        except (OSError, ValueError):
            # corrupt file: quarantine, never crash boot (same policy as Core)
            try:
                quarantine = self.state_path.with_suffix(".corrupt")
                self.state_path.rename(quarantine)
            except OSError:
                pass

    def maybe_flush(self, force: bool = False) -> None:
        now = time.time()
        if not force:
            if not self._dirty or (now - self._last_flush) < self._flush_interval:
                return
        with self._lock:
            if not self._dirty and not force:
                return
            self._data["saved_at"] = now
            snapshot = json.dumps(self._data, ensure_ascii=False, default=str)
            self._dirty = False
            self._last_flush = now
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(snapshot, encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---- per-engine logs -------------------------------------------------------
    def append_log(self, engine_name: str, line: str, keep: int = 200) -> None:
        with self._lock:
            bucket = self._logs.setdefault(engine_name, deque(maxlen=keep))
            bucket.append(line)
            if len(bucket) % 25 == 1:  # append to file periodically, not per line
                self._write_log_file(engine_name, list(bucket)[-25:])
        # keep memory bounded even when the ring wraps often
        with self._lock:
            if len(self._logs) > 40:
                for name in list(self._logs.keys())[:-40]:
                    self._logs.pop(name, None)

    def _write_log_file(self, engine_name: str, lines: list[str]) -> None:
        try:
            path = self.dir / LOG_DIR / f"{engine_name.lower()}.log"
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def engine_logs(self, engine_name: str, limit: int = 80) -> list[str]:
        with self._lock:
            bucket = self._logs.get(engine_name)
            return list(bucket or [])[-limit:]

    def data_dir(self) -> str:
        return str(self.dir)
