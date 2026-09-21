"""CDN / decoy-target scanner — /engines/sni_enhanced/scanner.

Server-side TCP+TLS probe of candidate endpoints (CDN anycast IPs, edge
nodes, decoy-SNI hosts). For each target it measures:

  * TCP reachability and connect latency (ms)
  * TLS behaviour — a real ClientHello carrying an allowed SNI is sent
    and the first response byte is checked for a TLS record header

Honest scope (documented in the panel): the scan runs from THIS Railway
deployment's vantage point. It proves a target is alive, fast and speaks
TLS with the probed SNI — it cannot prove the target is reachable from
inside Iran; that final test is the client's. The scan output is a ranked
candidate list for the client helper / CDN-fronted endpoints.

Budget guards (Railway free tier):
  * at most 32 targets per scan, 2.5s connect timeout each
  * every target probed in PARALLEL (one event loop, ~0 CPU)
  * minimum 30s between two scans (rate limit, env-tunable)
  * history capped to the last 10 scans (a few KB of state)
"""
from __future__ import annotations

import asyncio
import time

from .handshake_builder import build_fake_client_hello

MAX_TARGETS = 32
DEFAULT_TIMEOUT = 2.5
MIN_SCAN_INTERVAL = 30.0
HISTORY_CAP = 10

DEFAULT_TARGETS = (
    "1.1.1.1:443,1.0.0.1:443,8.8.8.8:443,8.8.4.4:443,9.9.9.9:443,"
    "cdnjs.cloudflare.com:443,hcaptcha.com:443"
)


def parse_targets(raw: str | list) -> list[tuple[str, int]]:
    """'host:port,host:port' (or a list) -> validated [(host, port)]."""
    if isinstance(raw, str):
        items = [s.strip() for s in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        items = []
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        if not item:
            continue
        host = item
        port = 443
        if ":" in item:
            host, _, port_s = item.rpartition(":")
            if not host or not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
                continue
            port = int(port_s)
        if not host or len(host) > 253 or "/" in host:
            continue
        if (host, port) not in seen:
            seen.add((host, port))
            out.append((host, port))
    return out[:MAX_TARGETS]


async def probe_target(host: str, port: int, *, sni: str = "",
                       timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One endpoint probe: TCP connect latency + TLS record check."""
    started = time.monotonic()
    result = {"target": f"{host}:{port}", "host": host, "port": port,
              "ok": False, "latency_ms": None, "tls": False, "error": ""}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
    except (asyncio.TimeoutError, OSError) as exc:
        result["error"] = type(exc).__name__ if isinstance(exc, OSError) else "timeout"
        return result
    latency = (time.monotonic() - started) * 1000.0
    result["latency_ms"] = round(latency, 1)
    try:
        try:
            hello = build_fake_client_hello(sni or host, "chrome")
            writer.write(hello)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            head = await asyncio.wait_for(reader.read(1), timeout=timeout)
            result["tls"] = bool(head) and head[:1] == b"\x16"
        except (asyncio.TimeoutError, OSError, ValueError):
            result["tls"] = False
        result["ok"] = True
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except (asyncio.TimeoutError, OSError, Exception):
            pass
    return result


class ScanRunner:
    """Rate-limited, history-capped scan orchestrator."""

    def __init__(self, *, min_interval: float = MIN_SCAN_INTERVAL):
        self.min_interval = max(5.0, float(min_interval))
        self.last_scan_at = 0.0
        self.history: list[dict] = []

    def _record(self, scan: dict) -> None:
        self.history.append(scan)
        self.history = self.history[-HISTORY_CAP:]

    def cached(self) -> dict | None:
        """Most recent scan, if any."""
        return self.history[-1] if self.history else None

    async def run(self, targets: list[tuple[str, int]], *, sni: str = "",
                  force: bool = False) -> dict:
        """Probe up to MAX_TARGETS endpoints in parallel (rate-limited)."""
        now = time.monotonic()
        if not force and now - self.last_scan_at < self.min_interval:
            last = self.cached()
            if last:
                return {**last, "rate_limited": True}
        if not targets:
            targets = parse_targets(DEFAULT_TARGETS)
        self.last_scan_at = now
        started = time.monotonic()
        results = await asyncio.gather(*[
            probe_target(host, port, sni=sni) for host, port in targets],
            return_exceptions=False)
        ok = [r for r in results if r.get("ok")]
        scan = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
            "probed": len(results),
            "alive": len(ok),
            "results": sorted(results, key=lambda r: (not r.get("ok"),
                                                       r.get("latency_ms") or 9e9)),
            "rate_limited": False,
        }
        self._record({"started_at": scan["started_at"], "probed": scan["probed"],
                     "alive": scan["alive"], "duration_ms": scan["duration_ms"],
                     "results": scan["results"][:MAX_TARGETS]})
        return scan
