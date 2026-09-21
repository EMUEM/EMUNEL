"""DpiMesh aggregator — pure, unit-testable policy derivation.

Takes the last hour of dpi_signatures rows and derives one policy per
(isp_hash, region_hash): the transport/protocol combination with the best
success rate (latency as the tie-break), its parameters, and a confidence
derived from the sample size.

Pure functions only — no I/O, no engine state, safe to test in isolation.
"""
from __future__ import annotations

import math
from collections import defaultdict

# A policy only becomes "confident" once this many samples back it.
CONFIDENCE_SAMPLE_TARGET = 30

VALID_RESULTS = ("ok", "blocked", "reset", "timeout", "fail")


def _jitter(latencies: list[float]) -> float:
    """Standard deviation of the observed latencies (the honest jitter
    approximation available from the signature table)."""
    if len(latencies) < 2:
        return 0.0
    avg = sum(latencies) / len(latencies)
    var = sum((x - avg) ** 2 for x in latencies) / len(latencies)
    return math.sqrt(max(0.0, var))


def group_rows(rows) -> dict:
    """rows: (isp_hash, region_hash, transport, protocol, result, latency)
    -> {(isp, region): {transport: {"ok": n, "total": n, "lat": [...],
                                    "protocol": str}}}
    """
    grouped: dict[tuple, dict] = defaultdict(dict)
    for isp, region, transport, protocol, result, latency in rows:
        transport = (str(transport) or "unknown")[:32]
        bucket = grouped[(str(isp), str(region))].setdefault(transport, {
            "ok": 0, "total": 0, "lat": [], "protocol": str(protocol or "")[:32],
        })
        bucket["total"] += 1
        if str(result).lower() == "ok":
            bucket["ok"] += 1
        try:
            lat = float(latency)
        except (TypeError, ValueError):
            lat = None
        if lat is not None and 0.0 <= lat <= 300_000.0:
            bucket["lat"].append(lat)
    return grouped


def build_policies(rows) -> list[dict]:
    """Derive one policy row per (isp_hash, region_hash).

    Recommended transport: highest success rate, lower average latency as
    the tie-break. Confidence: success rate scaled by sample coverage.
    """
    out: list[dict] = []
    for (isp_hash, region_hash), transports in group_rows(rows).items():
        scored = []
        for transport, agg in transports.items():
            total = agg["total"]
            success = (agg["ok"] / total) if total else 0.0
            avg_lat = (sum(agg["lat"]) / len(agg["lat"])) if agg["lat"] else None
            jitter = _jitter(agg["lat"])
            scored.append({
                "transport": transport,
                "protocol": agg["protocol"],
                "success_rate": round(success, 4),
                "avg_latency_ms": round(avg_lat, 2) if avg_lat is not None else None,
                "jitter_ms": round(jitter, 2),
                "samples": total,
                # success first, then faster average wins the tie
                "_rank": (success, -(avg_lat if avg_lat is not None else 1e9)),
            })
        scored.sort(key=lambda s: s["_rank"], reverse=True)
        best = scored[0]
        confidence = best["success_rate"] * min(
            1.0, best["samples"] / CONFIDENCE_SAMPLE_TARGET)
        out.append({
            "isp_hash": isp_hash,
            "region_hash": region_hash,
            "recommended_protocol": best["transport"],
            "recommended_params": {
                "protocol": best["protocol"],
                "success_rate": best["success_rate"],
                "avg_latency_ms": best["avg_latency_ms"],
                "jitter_ms": best["jitter_ms"],
                "samples": best["samples"],
                "alternatives": [
                    {"transport": s["transport"],
                     "success_rate": s["success_rate"]}
                    for s in scored[1:3]
                ],
            },
            "confidence": round(confidence, 4),
        })
    return out
