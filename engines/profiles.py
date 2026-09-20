"""Morphing engine support: ISP mapping, traffic profiles and the LinUCB bandit.

Profiles are named parameter sets describing how downlink frames are shaped
(chunk size, inter-chunk delay, optional padding). They come from
EMUNEL_MORPH_PROFILES (JSON) — an operator can define per-ISP profile lists;
the built-in default set below is conservative and safe.

The LinUCB bandit (one model per ISP) selects the profile with the best
predicted reward under exploration; rewards arrive as connection outcomes
observed by the gateway middleware (relay succeeded / died early). The
matrices are tiny (context dim 4), persisted through the engine state store,
and trained entirely in-process — well within a free-tier CPU budget.
"""
from __future__ import annotations

import ipaddress
import math
import time
from dataclasses import dataclass, field

# ---- default profiles ---------------------------------------------------------
# chunk: split downlink writes into <= chunk bytes (TCP segmentation shaping)
# delay_ms: pause between chunks (pacing; 0 = off)
# padding: max random padding per batch (bytes) to fit size signatures
# fp_hint / sni / alpn are echoed into generated client configs as hints.
DEFAULT_PROFILES: dict[str, list[dict]] = {
    "*": [
        {"name": "baseline", "chunk": 0, "delay_ms": 0, "padding": 0},
        {"name": "youtube-like", "chunk": 32768, "delay_ms": 0, "padding": 0,
         "sizes": [1400, 1300, 1200, 1100], "burst": 12, "gap_ms": 2},
        {"name": "zoom-like", "chunk": 1200, "delay_ms": 1, "padding": 0,
         "sizes": [1200], "burst": 4, "gap_ms": 20},
        {"name": "telegram-like", "chunk": 4096, "delay_ms": 0, "padding": 0,
         "sizes": [1400, 800, 600], "burst": 6, "gap_ms": 5},
    ],
}


def normalize_profiles(raw: dict) -> dict[str, list[dict]]:
    """Validate operator-provided profiles (JSON), falling back to defaults."""
    out: dict[str, list[dict]] = {}
    if not isinstance(raw, dict):
        return {k: [dict(p) for p in v] for k, v in DEFAULT_PROFILES.items()}
    for isp, profiles in raw.items():
        if not isinstance(profiles, list):
            continue
        cleaned = []
        for p in profiles:
            if not isinstance(p, dict) or "name" not in p:
                continue
            cleaned.append({
                "name": str(p["name"])[:40],
                "chunk": max(0, int(p.get("chunk", 0) or 0)),
                "delay_ms": max(0, float(p.get("delay_ms", 0) or 0)),
                "padding": max(0, int(p.get("padding", 0) or 0)),
                "sizes": [int(s) for s in (p.get("sizes") or []) if 0 < int(s) <= 65535][:8],
                "burst": max(1, int(p.get("burst", 1) or 1)),
                "gap_ms": max(0.0, float(p.get("gap_ms", 0) or 0)),
            })
        if cleaned:
            out[str(isp)] = cleaned
    if "*" not in out or not out.get("*"):
        out["*"] = [dict(p) for p in DEFAULT_PROFILES["*"]]
    return out


# ---- ISP mapping ---------------------------------------------------------------
def build_prefix_table(prefix_map: dict[str, str]) -> list[tuple[int, int, str]]:
    """`{"1.2.0.0/16": "mci"}` -> [(network_int, prefixlen, isp)] sorted longest-first."""
    table = []
    for prefix, isp in (prefix_map or {}).items():
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            continue
        table.append((int(net.network_address), net.prefixlen, isp))
    table.sort(key=lambda t: -t[1])  # longest prefix wins
    return table


def isp_for_ip(ip: str, table: list[tuple[int, int, str]]) -> str:
    if not table or not ip:
        return "*"
    try:
        addr = ipaddress.ip_address(ip.split(",")[0].strip())
    except ValueError:
        return "*"
    if addr.version != 4:
        return "*"
    value = int(addr)
    for network_int, prefixlen, isp in table:
        if prefixlen == 0:
            return isp
        mask = (0xFFFFFFFF << (32 - prefixlen)) & 0xFFFFFFFF
        if (value & mask) == (network_int & mask):
            return isp
    return "*"


# ---- LinUCB ------------------------------------------------------------------------
@dataclass
class Arm:
    name: str
    d: int
    a: list[list[float]] = field(default_factory=list)   # A matrix (d x d)
    b: list[float] = field(default_factory=list)        # b vector
    pulls: int = 0
    rewards: float = 0.0

    def __post_init__(self):
        if not self.a:
            self.a = [[1.0 if i == j else 0.0 for j in range(self.d)] for i in range(self.d)]
            self.b = [0.0] * self.d


def _identity(d: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(d)] for i in range(d)]


def _mat_inverse_add(a: list[list[float]], lam: float = 1e-6) -> list[list[float]]:
    """Small closed-form inversion via Gauss-Jordan (d<=6 — cheap and stable
    enough for bandit parameters; not a general linear algebra library)."""
    d = len(a)
    m = [row[:] + ident_row for row, ident_row in zip(a, _identity(d))]
    for col in range(d):
        pivot = max(range(col, d), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            m[col][col] += lam
            pivot = col
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col] or 1.0
        m[col] = [v / div for v in m[col]]
        for r in range(d):
            if r != col and m[r][col] != 0.0:
                factor = m[r][col]
                m[r] = [rv - factor * cv for rv, cv in zip(m[r], m[col])]
    return [row[d:] for row in m]


class LinUCB:
    """Disjoint-model LinUCB, context dim = 4:
    [1, loss_ewma, rtt_ewma/100ms, hour_cos] — enough signal, tiny state."""

    CONTEXT_DIM = 4

    def __init__(self, alpha: float):
        self.alpha = alpha
        self.arms: dict[str, Arm] = {}

    # ---- persistence ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "arms": {
                name: {"a": arm.a, "b": arm.b, "pulls": arm.pulls, "rewards": arm.rewards}
                for name, arm in self.arms.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict, alpha: float) -> "LinUCB":
        bandit = cls(alpha)
        if isinstance(raw, dict) and isinstance(raw.get("arms"), dict):
            for name, payload in raw["arms"].items():
                try:
                    arm = Arm(name=str(name), d=cls.CONTEXT_DIM)
                    arm.a = [[float(v) for v in row] for row in payload.get("a", [])] or _identity(cls.CONTEXT_DIM)
                    arm.b = [float(v) for v in payload.get("b", [])] or [0.0] * cls.CONTEXT_DIM
                    arm.pulls = int(payload.get("pulls", 0) or 0)
                    arm.rewards = float(payload.get("rewards", 0.0) or 0.0)
                    if len(arm.a) == cls.CONTEXT_DIM and len(arm.b) == cls.CONTEXT_DIM:
                        bandit.arms[name] = arm
                except (TypeError, ValueError):
                    continue
        return bandit

    # ---- learning -----------------------------------------------------------------
    @staticmethod
    def context(loss_ewma: float = 0.0, rtt_ewma_s: float = 0.0) -> list[float]:
        hour = time.localtime().tm_hour + time.localtime().tm_min / 60.0
        return [1.0,
                max(-1.0, min(1.0, loss_ewma)),
                max(0.0, min(2.0, rtt_ewma_s / 0.1)),
                math.cos(2 * math.pi * hour / 24.0)]

    def choose(self, arm_names: list[str], context: list[float]) -> tuple[str, float]:
        if not arm_names:
            return "", 0.0
        ctx = (context or [1.0] + [0.0] * (self.CONTEXT_DIM - 1))[:self.CONTEXT_DIM]
        while len(ctx) < self.CONTEXT_DIM:
            ctx.append(0.0)
        best, best_score = arm_names[0], -1e18
        for name in arm_names:
            arm = self.arms.setdefault(name, Arm(name=name, d=self.CONTEXT_DIM))
            inv = _mat_inverse_add(arm.a)
            theta = [sum(inv[i][j] * arm.b[j] for j in range(self.CONTEXT_DIM))
                     for i in range(self.CONTEXT_DIM)]
            mean = sum(t * c for t, c in zip(theta, ctx))
            var = sum(ctx[i] * sum(inv[i][j] * ctx[j] for j in range(self.CONTEXT_DIM))
                      for i in range(self.CONTEXT_DIM))
            bonus = self.alpha * math.sqrt(max(0.0, var))
            score = mean + bonus
            if score > best_score:
                best, best_score = name, score
        return best, best_score

    def update(self, arm_name: str, context: list[float], reward: float) -> None:
        if arm_name not in self.arms:
            self.arms[arm_name] = Arm(name=arm_name, d=self.CONTEXT_DIM)
        arm = self.arms[arm_name]
        ctx = (context or [1.0] + [0.0] * (self.CONTEXT_DIM - 1))[:self.CONTEXT_DIM]
        while len(ctx) < self.CONTEXT_DIM:
            ctx.append(0.0)
        for i in range(self.CONTEXT_DIM):
            for j in range(self.CONTEXT_DIM):
                arm.a[i][j] += ctx[i] * ctx[j]
            arm.b[i] += max(0.0, min(1.0, reward)) * ctx[i]
        arm.pulls += 1
        arm.rewards += max(0.0, min(1.0, reward))
