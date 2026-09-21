"""SNI pool manager for the enhanced SNI engine — /engines/sni_enhanced/sni_pool.

Loads the allowed-SNI list (ir_allowed_snis.json by default), validates
operator edits, and picks entries with weighted randomness. Weights start
uniform and adapt: the engine feeds back per-SNI success/failure from
validated plans and client reports, so decoy SNIs that keep working are
preferred — bounded to [0.25, 4.0] x uniform so no entry ever disappears
or dominates.

Everything is pure stdlib, in-memory (a few KB) and synchronous: Railway
budget rules (<30MB RAM, <3% CPU) are respected by design.
"""
from __future__ import annotations

import json
import random
import re
import threading
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent
DEFAULT_JSON = _DATA_DIR / "ir_allowed_snis.json"

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$",
                      re.IGNORECASE)

MIN_WEIGHT = 0.25
MAX_WEIGHT = 4.0


def validate_sni(host: str) -> str | None:
    """Return the cleaned hostname, or None when invalid."""
    host = str(host or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or "://" in host:
        return None
    return host if _HOST_RE.match(host) else None


class SNIPool:
    """Weighted, self-adjusting pool of decoy SNIs."""

    def __init__(self, snis: list[str] | None = None):
        self._lock = threading.Lock()
        self._weights: dict[str, float] = {}
        self._stats: dict[str, dict] = {}
        entries = [validate_sni(s) for s in (snis or [])]
        self._entries: list[str] = []
        for entry in entries:
            if entry and entry not in self._entries:
                self._entries.append(entry)
        if not self._entries:
            self._entries = load_default_snis()

    # ---- contents ---------------------------------------------------------
    def list(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def replace(self, snis: list[str]) -> list[str]:
        """Operator edit: validate, de-duplicate (cap 64) and swap in."""
        clean: list[str] = []
        for raw in snis or []:
            host = validate_sni(raw)
            if not host:
                raise ValueError(f"invalid SNI entry: {raw!r}")
            if host not in clean:
                clean.append(host)
        if not clean:
            raise ValueError("SNI pool cannot be empty")
        clean = clean[:64]
        with self._lock:
            self._entries = clean
            self._weights = {k: v for k, v in self._weights.items() if k in clean}
        return list(clean)

    # ---- selection ---------------------------------------------------------
    def pick(self, rng: random.Random | None = None) -> str:
        """Weighted random pick; falls back to a fixed decoy if empty."""
        rng = rng or random.Random()
        with self._lock:
            if not self._entries:
                return "www.varzesh3.com"
            weights = [self._weights.get(s, 1.0) for s in self._entries]
            return rng.choices(self._entries, weights=weights, k=1)[0]

    # ---- adaptation ----------------------------------------------------------
    def report(self, sni: str, ok: bool) -> None:
        """Feed one outcome back; nudges the weight within bounds."""
        sni = validate_sni(sni) or ""
        if not sni:
            return
        with self._lock:
            if sni not in self._entries:
                return
            stat = self._stats.setdefault(sni, {"ok": 0, "fail": 0})
            stat["ok" if ok else "fail"] += 1
            current = self._weights.get(sni, 1.0)
            self._weights[sni] = min(MAX_WEIGHT, max(MIN_WEIGHT,
                                                     current * (1.05 if ok else 0.95)))

    def snapshot(self) -> dict:
        """Pool + adaptive stats for the panel."""
        with self._lock:
            return {
                "count": len(self._entries),
                "snis": list(self._entries),
                "stats": {
                    s: {
                        "weight": round(self._weights.get(s, 1.0), 2),
                        "ok": self._stats.get(s, {}).get("ok", 0),
                        "fail": self._stats.get(s, {}).get("fail", 0),
                    }
                    for s in self._entries
                },
            }


def load_default_snis() -> list[str]:
    """Read ir_allowed_snis.json; never raises (hardcoded fallback)."""
    try:
        raw = json.loads(DEFAULT_JSON.read_text(encoding="utf-8"))
        snis = [validate_sni(s) for s in (raw.get("allowed_snis") or [])]
        return [s for s in snis if s] or ["www.varzesh3.com"]
    except (OSError, ValueError):
        return ["www.varzesh3.com"]


def load_strategies() -> dict:
    """Read ISP_STRATEGIES.json; never raises (minimal fallback)."""
    try:
        raw = json.loads((_DATA_DIR / "ISP_STRATEGIES.json").read_text(encoding="utf-8"))
        strategies = raw.get("strategies")
        if isinstance(strategies, dict) and strategies:
            return strategies
    except (OSError, ValueError):
        pass
    return {"auto": {"label": "Auto", "method": "combined", "fooling": "md5sig",
                     "repeats": 6, "seqovl": 568, "split_pos": 1, "midsld": True,
                     "fingerprint": "chrome"}}
