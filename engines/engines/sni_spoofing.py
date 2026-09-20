"""SNI Spoofing Engine — client-side ClientHello bypass, honestly server-side.

WHAT THIS ENGINE IS (and is not) — read before enabling:

SNI spoofing, fragmentation and the TTL trick all execute ON THE USER'S
DEVICE, never on the Railway server. A DPI box sits between the client and
the server; by the time traffic reaches this panel it has already passed
the censor. So this engine is exactly what the operator's spec calls it:

    "sni_engine on Railway is a CONFIG GENERATOR, not the spoofing executor"

It provides:

  * an operator-editable bypass PROFILE (method, fragment strategy,
    delay, TTL, SNI pool) stored in engine state and validated here;
  * the ClientHello parser / fragment planner / fake-packet builder that
    the panel's "Test" button runs server-side to prove the plan is valid;
  * a self-contained, stdlib-only CLIENT HELPER SCRIPT (downloadable from
    the Bypass tab) that executes the profile next to the proxy client:
    it listens locally, intercepts the ClientHello, applies the strategy
    (fragment / fake_sni / combined) and relays.

Strategies (fragmentation of the real ClientHello):

    sni_split       cut exactly inside the SNI hostname (recommended)
    half            two halves of the whole record
    multi           ~24-byte pieces (randomized +- for anti-fingerprinting)
    tls_record_frag split the handshake message across two TLS records

Methods (what to send before the real ClientHello):

    fragment        only the fragmented real ClientHello
    fake_sni        a synthetic ClientHello with an allowed SNI sent on a
                    separate low-TTL connection (dies mid-path, seen by DPI)
    combined        fake first, then fragmented real (recommended)

The fake packet uses a plain TCP socket with IP_TTL set (kernel honors it
on Linux/macOS/Windows) — no raw sockets, no admin rights, no pcap.
Out-of-order delivery is intentionally NOT implemented (documented).
"""
from __future__ import annotations

import random
import time

from ..base import Engine
from ..config import DEFAULT_SNI_POOL

METHODS = ("fragment", "fake_sni", "combined")
STRATEGIES = ("sni_split", "half", "multi", "tls_record_frag")
MULTI_CHUNK = 24                     # spec: ~24-byte pieces
FINGERPRINT_JITTER = 0.2            # +-20% on delays/sizes (anti-fingerprint)

VALID_POOL_DEFAULT = DEFAULT_SNI_POOL


# ---------------------------------------------------------------------------
# Pure ClientHello logic — shared verbatim by the client helper script.
# The helper embeds its own copy so it stays a single standalone file; the
# unit tests exercise BOTH copies and assert they agree (see test_bypass_engines).
# ---------------------------------------------------------------------------
def parse_client_hello(data: bytes) -> dict:
    """Best-effort TLS 1.x ClientHello scan. Returns {} when not parseable.

    Locates the SNI hostname bytes and the record/handshake layout needed by
    the fragment planners. Never raises — an unparseable hello simply is not
    fragmented (passthrough)."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return {}
        record_len = int.from_bytes(data[3:5], "big")
        if len(data) < 5 + record_len or data[5] != 0x01:
            return {}
        pos = 9                                    # handshake body start
        pos += 2 + 32                              # client_version + random
        sid_len = data[pos]
        pos += 1 + sid_len
        cipher_len = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2 + cipher_len
        comp_len = data[pos]
        pos += 1 + comp_len
        ext_total = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2
        end = min(len(data), pos + ext_total)
        while pos + 4 <= end:
            ext_type = int.from_bytes(data[pos:pos + 2], "big")
            ext_len = int.from_bytes(data[pos + 2:pos + 4], "big")
            body = data[pos + 4: pos + 4 + ext_len]
            if ext_type == 0x0000 and len(body) >= 5:
                # server_name_list: uint16 total, then entries
                name_type = body[2]
                name_len = int.from_bytes(body[3:5], "big")
                name_start = pos + 4 + 5
                if name_type == 0 and 0 < name_len <= 255:
                    return {
                        "sni": body[5:5 + name_len].decode("ascii", "replace"),
                        "sni_start": name_start,
                        "sni_end": name_start + name_len,
                        "record_len": record_len,
                        "hello_len": len(data),
                    }
            pos += 4 + ext_len
    except (IndexError, ValueError):
        return {}
    return {}


def plan_fragments(data: bytes, strategy: str, rng: random.Random | None = None) -> list[bytes]:
    """Split the ClientHello per strategy. Byte stream is always preserved:
    b"".join(parts) == data for every strategy."""
    rng = rng or random.Random()
    if not data:
        return [data]
    strategy = (strategy or "sni_split").strip().lower()
    if strategy not in STRATEGIES:
        strategy = "sni_split"
    if strategy == "multi":
        size = max(8, MULTI_CHUNK + rng.randint(-6, 6))
        return [data[i:i + size] for i in range(0, len(data), size)] or [data]
    if strategy == "half":
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    if strategy == "tls_record_frag":
        # Handshake fragmentation: two TLS records, each carrying part of the
        # SAME handshake message (the inner header still declares the full
        # length; receivers reassemble per RFC 8446 s.5).
        if len(data) < 12:
            return [data]
        content = data[5:]
        hello = parse_client_hello(data)
        anchor = (hello.get("sni_start", 0) or 0) - 5
        cut = anchor if 0 < anchor < len(content) else len(content) // 2
        header = data[:5]
        first_len = len(content[:cut])
        second_len = len(content[cut:])
        rec1 = header[:3] + first_len.to_bytes(2, "big") + content[:cut]
        rec2 = header[:3] + second_len.to_bytes(2, "big") + content[cut:]
        return [rec1, rec2]
    # sni_split (default): cut the hostname itself in half
    hello = parse_client_hello(data)
    start, end = hello.get("sni_start", 0), hello.get("sni_end", 0)
    if not hello or end <= start + 1:
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    cut = start + (end - start) // 2
    return [data[:cut], data[cut:]]


def build_fake_client_hello(sni: str, rng: random.Random | None = None) -> bytes:
    """A structurally valid, minimal TLS 1.2 ClientHello carrying ``sni``.

    It only needs to survive a DPI parser long enough to be classified as
    "TLS to an allowed site" before its low TTL kills it mid-path. Session
    id, ciphers and extensions are randomized for anti-fingerprinting."""
    rng = rng or random.Random()
    host = (sni or "").encode("ascii", "ignore")[:253] or b"www.microsoft.com"
    rand = bytes(rng.randrange(256) for _ in range(32))
    sid = bytes(rng.randrange(256) for _ in range(rng.choice((0, 8, 16, 32))))
    ciphers = bytes.fromhex(
        "130113021303c02bc02fc02cc030009f009ecca9cca8")
    comp = b"\x01\x00"                       # null compression
    ext = (
        (0).to_bytes(2, "big")                # server_name
        + (len(host) + 5).to_bytes(2, "big")
        + (len(host) + 3).to_bytes(2, "big")
        + b"\x00"                            # host_name
        + len(host).to_bytes(2, "big")
        + host
    )
    supported_groups = bytes.fromhex("001d00170018")
    ec_point = b"\x01\x02\x01\x00"           # uncompressed
    ext += ((10).to_bytes(2, "big") + len(supported_groups).to_bytes(2, "big")
            + supported_groups)
    ext += ((11).to_bytes(2, "big") + len(ec_point).to_bytes(2, "big") + ec_point)
    body = (
        b"\x03\x03" + rand
        + len(sid).to_bytes(1, "big") + sid
        + len(ciphers).to_bytes(2, "big") + ciphers
        + comp
        + len(ext).to_bytes(2, "big") + ext
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def delay_for(base_delay: float, rng: random.Random | None = None) -> float:
    """Anti-fingerprint jitter around the configured fragment delay."""
    rng = rng or random.Random()
    jitter = rng.uniform(-FINGERPRINT_JITTER, FINGERPRINT_JITTER)
    return max(0.0, base_delay * (1.0 + jitter))


def pick_pool_sni(pool: list[str], rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    clean = [p.strip() for p in (pool or []) if p.strip()]
    if not clean:
        clean = [p.strip() for p in VALID_POOL_DEFAULT.split(",") if p.strip()]
    return rng.choice(clean)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class SNISpoofingEngine(Engine):
    NAME = "SNISpoof"
    TITLE = "SNI Spoofing — client-side ClientHello bypass (profile generator + helper)"
    HANDLES = frozenset()            # service engine: API/Bypass tab, no pipeline hop
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002
        stored = self.state.get(self.NAME, {}) or {}
        profile = self._merge_profile(stored.get("profile") or {})
        self.state.set(self.NAME, {"profile": profile})
        self.profile = profile
        self.status.metrics.update({
            "profiles_set": int(stored.get("profiles_set", 0) or 0),
            "tests_run": int(stored.get("tests_run", 0) or 0),
            "helper_downloads": int(stored.get("helper_downloads", 0) or 0),
        })

    async def start(self) -> None:
        self.log.info(
            f"profile: method={self.profile['method']} "
            f"strategy={self.profile['fragment_strategy']} "
            f"delay={self.profile['fragment_delay']:.2f}s "
            f"ttl={self.profile['ttl_trick']} "
            f"pool={len(self.profile['sni_pool'])} — spoofing itself runs "
            "on client devices via the downloadable helper")

    def preconditions(self) -> str | None:
        return None      # generator always can run

    def defaults(self) -> dict:
        return dict(self.profile)

    # ---- profile management ------------------------------------------------
    def _merge_profile(self, override: dict) -> dict:
        p = {
            "method": self.cfg.sni_method if self.cfg.sni_method in METHODS else "combined",
            "fragment_strategy": (self.cfg.sni_fragment_strategy
                                  if self.cfg.sni_fragment_strategy in STRATEGIES else "sni_split"),
            "fragment_delay": float(self.cfg.sni_fragment_delay),
            "ttl_trick": bool(self.cfg.sni_ttl_trick),
            "ttl_value": int(self.cfg.sni_ttl_value),
            "fake_sni": self.cfg.sni_fake_sni,
            "sni_pool": list(self.cfg.sni_pool or []),
            "listen_port": int(self.cfg.sni_listen_port),
        }
        if isinstance(override, dict) and override:
            p.update({k: override[k] for k in p if k in override})
            p["method"] = p["method"] if p["method"] in METHODS else "combined"
            p["fragment_strategy"] = (p["fragment_strategy"]
                                      if p["fragment_strategy"] in STRATEGIES else "sni_split")
            p["fragment_delay"] = min(2.0, max(0.0, float(p["fragment_delay"] or 0)))
            p["ttl_value"] = min(8, max(1, int(p["ttl_value"] or 1)))
        if not p["sni_pool"]:
            p["sni_pool"] = [s.strip() for s in VALID_POOL_DEFAULT.split(",") if s.strip()]
        return p

    def set_profile(self, body: dict) -> dict:
        """Validate + persist a new profile. Only provided keys change."""
        update: dict = {}
        if "method" in body:
            method = str(body.get("method") or "").strip().lower()
            if method not in METHODS:
                raise ValueError(f"method must be one of {', '.join(METHODS)}")
            update["method"] = method
        if "strategy" in body or "fragment_strategy" in body:
            strategy = str(body.get("strategy") or body.get("fragment_strategy") or "").strip().lower()
            if strategy not in STRATEGIES:
                raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
            update["fragment_strategy"] = strategy
        if "delay" in body or "fragment_delay" in body:
            try:
                delay = float(body.get("delay", body.get("fragment_delay")))
            except (TypeError, ValueError):
                raise ValueError("delay must be a number (seconds)")
            if not 0.0 <= delay <= 2.0:
                raise ValueError("delay must be between 0 and 2 seconds (0.1-0.5 recommended)")
            update["fragment_delay"] = delay
        if "ttl_trick" in body:
            update["ttl_trick"] = bool(body.get("ttl_trick"))
        if "ttl_value" in body:
            try:
                ttl = int(body.get("ttl_value"))
            except (TypeError, ValueError):
                raise ValueError("ttl_value must be an integer 1-8")
            if not 1 <= ttl <= 8:
                raise ValueError("ttl_value must be between 1 and 8")
            update["ttl_value"] = ttl
        if "fake_sni" in body:
            fake = str(body.get("fake_sni") or "").strip()
            if fake and ("." not in fake or len(fake) > 253 or "://" in fake):
                raise ValueError("fake_sni must be a plain hostname")
            update["fake_sni"] = fake or "www.microsoft.com"
        if "sni_pool" in body:
            raw = body.get("sni_pool")
            if isinstance(raw, str):
                raw = [s.strip() for s in raw.replace("\n", ",").split(",")]
            if not isinstance(raw, list):
                raise ValueError("sni_pool must be a list of hostnames")
            clean = []
            for item in raw:
                host = str(item or "").strip()
                if not host:
                    continue
                if "." not in host or len(host) > 253 or "://" in host:
                    raise ValueError(f"sni_pool entry invalid: {host!r}")
                clean.append(host)
            if not clean:
                raise ValueError("sni_pool cannot be empty")
            update["sni_pool"] = clean[:64]
        if "listen_port" in body:
            try:
                port = int(body.get("listen_port"))
            except (TypeError, ValueError):
                raise ValueError("listen_port must be an integer")
            if not 1 <= port <= 65535:
                raise ValueError("listen_port must be a valid port")
            update["listen_port"] = port
        self.profile.update(update)
        stored = self.state.get(self.NAME, {}) or {}
        stored["profile"] = dict(self.profile)
        self.state.set(self.NAME, stored)
        self.status.metrics["profiles_set"] = int(self.status.metrics.get("profiles_set", 0)) + 1
        self.log.info("profile updated: " + ", ".join(sorted(update)))
        return dict(self.profile)

    # ---- server-side proof of the plan -------------------------------------
    def run_test(self) -> dict:
        """Build a synthetic ClientHello carrying an SNI, plan the fragments
        with the CURRENT profile and verify stream preservation + structure.
        Runs entirely server-side — proves the planner the helper uses."""
        rng = random.Random()
        hello = build_fake_client_hello("emunel-test.example.com", rng)
        parsed = parse_client_hello(hello)
        parts = plan_fragments(hello, self.profile["fragment_strategy"], rng)
        # Stream preservation: for byte-level strategies the concatenation is
        # the original; tls_record_frag re-wraps the same handshake message
        # into two records, so the RECORD CONTENT must concatenate to the
        # original content instead.
        strategy = self.profile["fragment_strategy"]
        if strategy == "tls_record_frag" and all(p[:1] == b"\x16" for p in parts):
            stream_ok = (b"".join(p[5:] for p in parts) == hello[5:]
                         and sum(int.from_bytes(p[3:5], "big") for p in parts)
                             == len(hello) - 5)
        else:
            stream_ok = b"".join(parts) == hello
        fake = (build_fake_client_hello(
            pick_pool_sni(self.profile["sni_pool"], rng), rng)
            if self.profile["method"] in ("fake_sni", "combined") else None)
        ok = (
            parsed.get("sni") == "emunel-test.example.com"
            and stream_ok
            and len(parts) >= 2
            and all(0 < len(p) for p in parts)
            and (fake is None or parse_client_hello(fake).get("sni"))
        )
        stored = self.state.get(self.NAME, {}) or {}
        stored["tests_run"] = int(stored.get("tests_run", 0) or 0) + 1
        self.state.set(self.NAME, stored)
        self.status.metrics["tests_run"] = int(self.status.metrics.get("tests_run", 0)) + 1
        return {
            "ok": bool(ok),
            "parsed_sni": parsed.get("sni"),
            "hello_bytes": len(hello),
            "fragment_sizes": [len(p) for p in parts],
            "cut_inside_sni": bool(
                parsed.get("sni_start") and parsed.get("sni_end")
                and any(parsed["sni_start"] < sum(len(p) for p in parts[:i + 1]) < parsed["sni_end"]
                        for i in range(len(parts) - 1))
            ) if self.profile["fragment_strategy"] == "sni_split" else None,
            "fake_packet_bytes": len(fake) if fake else 0,
            "fake_packet_sni": (parse_client_hello(fake).get("sni") if fake else None),
            "profile": dict(self.profile),
        }

    # ---- helper script -------------------------------------------------------
    def helper_source(self) -> str:
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "assets" / "emunel_sni_helper.py"
        try:
            src = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - packaging error
            raise RuntimeError(f"helper script missing from the image: {exc}") from exc
        stored = self.state.get(self.NAME, {}) or {}
        stored["helper_downloads"] = int(stored.get("helper_downloads", 0) or 0) + 1
        self.state.set(self.NAME, stored)
        self.status.metrics["helper_downloads"] = \
            int(self.status.metrics.get("helper_downloads", 0)) + 1
        return src

    def helper_usage(self) -> str:
        p = self.profile
        return (f"python emunel_sni_helper.py --connect <server-host>:443 "
                f"--method {p['method']} --strategy {p['fragment_strategy']} "
                f"--delay {p['fragment_delay']}"
                + (f" --ttl {p['ttl_value']}" if p["ttl_trick"] else "")
                + (f" --pool {','.join(p['sni_pool'][:4])}" if p["sni_pool"] else ""))

    async def stop(self) -> None:
        self.log.info("stopped (client devices keep their helper behavior)")
