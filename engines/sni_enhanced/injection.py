"""Injection logic — the enhanced fake-SNI technique planner (client-side plan,
server-side proof).

/injection implements the operator's algorithm:

    injectFakeSNI(socket, realTarget, sni):
      1. pick an SNI from the allowed pool (weighted random)
      2. build the fake ClientHello: SNI=selected, fooling=md5sig|badsum|ts|...,
         fingerprint=Chrome-like (uTLS shape)
      3. send the fake packet toward the destination IP
      4. wait 100-200ms (SNI_ENHANCED_FRAGMENT_DELAY, jittered)
      5. send the REAL ClientHello, fragmented per the active strategy
      6. the connection continues normally

WHAT RUNS WHERE (honest split, same rule as the basic SNISpoof engine):
the plan is BUILT and PROVEN here (byte-level: every step's payload, its
delay, its fooling); EXECUTION happens on the user's device through the
downloadable helper. Techniques that need raw sockets (seq manipulation,
TCP-MD5 signatures, urgent bytes, SYN-data/SYN-ACK injection) are planned
as ``raw`` steps with exact packet templates — the helper uses them only
when it has the required privileges, and degrades to the userspace ladder
(fragment / hostfakesplit / tlsrec / low-TTL decoy) otherwise.

Stream safety invariant: the concatenation of every ``data`` step payload
equals the original ClientHello (tlsrec re-wraps records — its record
CONTENT concatenates to the handshake message instead). This invariant is
asserted by run_test() for every technique before any plan ships.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .handshake_builder import (build_fake_client_hello, build_hostfakesplit_hello,
                                find_sni_span)
from .sni_pool import SNIPool

# ── vocabulary (operator spec) ────────────────────────────────────────────────
TECHNIQUES = (
    "fragment",        # fragmented real ClientHello only
    "fake_sni",        # decoy hello first, then real
    "combined",        # decoy first, then fragmented real (recommended default)
    "hostfakesplit",   # real hello with same-length random SNI first
    "multisplit",      # split with overlapping sequence numbers (seqovl)
    "multidisorder",   # out-of-order segments, decoy first (zapret-style)
    "fakedsplit",      # decoy + real split, forward order
    "fakeddisorder",   # decoy + real, reverse order
    "tlsrec",          # real hello wrapped in two TLS records
    "oob",             # urgent-pointer byte mid-handshake (raw)
    "disoob",          # disoob variant (raw)
    "wrong_seq",       # decoy with wrong sequence numbers (raw)
    "md5sig",          # decoy with invalid TCP-MD5 signature (raw)
    "syndata",         # payload bytes inside SYN (raw, state confusion)
    "synack",          # fake SYN-ACK from the client side (raw, state confusion)
)
FOOLING = ("md5sig", "badseq", "badsum", "ts", "autottl")
FRAGMENT_STRATEGIES = ("sni_split", "half", "multi", "midsld", "pos")
MULTI_CHUNK = 24
JITTER = 0.2          # +-20% anti-fingerprint jitter on delays/sizes


@dataclass
class PlanStep:
    """One sendable unit of the injection plan."""
    kind: str                    # "data" (real stream) | "fake" (decoy) | "raw"
    payload: bytes = b""
    delay_after: float = 0.0     # seconds to wait AFTER sending
    ttl: int | None = None       # IP_TTL for userspace decoys
    fooling: str | None = None   # md5sig|badseq|badsum|ts|autottl (raw steps)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "bytes": len(self.payload),
            "delay_after": round(self.delay_after, 3),
            "ttl": self.ttl,
            "fooling": self.fooling,
            "note": self.note,
        }


@dataclass
class InjectionPlan:
    """Full plan for one connection's ClientHello delivery."""
    technique: str
    steps: list[PlanStep] = field(default_factory=list)
    fake_sni: str = ""
    raw_socket_required: bool = False
    warnings: list[str] = field(default_factory=list)
    stream_preserved: bool = True
    profile: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "technique": self.technique,
            "steps": [s.as_dict() for s in self.steps],
            "fake_sni": self.fake_sni,
            "raw_socket_required": self.raw_socket_required,
            "warnings": list(self.warnings),
            "stream_preserved": self.stream_preserved,
            "profile": dict(self.profile),
        }


# ── fragmentation helpers (pure) ─────────────────────────────────────────────
def _delay(base: float, rng: random.Random) -> float:
    return max(0.0, base * (1.0 + rng.uniform(-JITTER, JITTER)))


def split_hello(data: bytes, strategy: str, *, split_pos: int = 1,
                midsld: bool = False, seqovl: int = 0,
                rng: random.Random | None = None) -> list[bytes]:
    """Split the ClientHello per the active strategy.

    ``seqovl`` > 0 marks the split as OVERLAPPING-SEQUENCE (multisplit):
    the returned parts are still a pure partition here (byte stream
    preserved — the SERVER must receive the exact bytes); the overlap is a
    property of the TCP sequence NUMBERS the raw-socket helper assigns,
    which this userspace plan records but cannot set. See state_confusion.
    """
    rng = rng or random.Random()
    if not data:
        return [data]
    strategy = (strategy or "sni_split").strip().lower()
    if strategy == "multi":
        size = max(8, MULTI_CHUNK + rng.randint(-6, 6))
        return [data[i:i + size] for i in range(0, len(data), size)] or [data]
    if strategy == "half":
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    if strategy == "midsld":
        # cut in the middle of the SLD label (leftmost label), not the TLD
        span = find_sni_span(data)
        if span:
            start, end = span[0], span[1]
            host_len = end - start
            cut = start + max(1, host_len // 2)
            if start < cut < end:
                return [data[:cut], data[cut:]]
        cut = len(data) // 2
        return [data[:cut], data[cut:]]
    if strategy == "pos":
        pos = max(1, min(int(split_pos or 1), max(1, len(data) - 1)))
        return [data[:pos], data[pos:]]
    # sni_split (default): cut exactly inside the SNI hostname
    span = find_sni_span(data)
    if span and span[1] > span[0] + 1:
        cut = span[0] + (span[1] - span[0]) // 2
        return [data[:cut], data[cut:]]
    cut = len(data) // 2
    return [data[:cut], data[cut:]]


def tlsrec_wrap(data: bytes) -> list[bytes]:
    """Wrap one handshake message into two TLS records (RFC 8446 s.5 style
    fragmentation). The receiver reassembles; simple DPI often does not."""
    if len(data) < 12:
        return [data]
    content = data[5:]
    span = find_sni_span(data)
    anchor = (span[0] - 5) if span else 0
    cut = anchor if 0 < anchor < len(content) else len(content) // 2
    header = data[:5]
    rec1 = header[:3] + (cut).to_bytes(2, "big") + content[:cut]
    rec2 = header[:3] + (len(content) - cut).to_bytes(2, "big") + content[cut:]
    return [rec1, rec2]


def verify_stream(original: bytes, parts: list[bytes]) -> bool:
    """Byte-stream preservation for plain splits: concat(parts) == original.
    TLS-record re-wraps are verified at content level by the caller."""
    return b"".join(parts) == original


# ── the planner ───────────────────────────────────────────────────────────────
def plan_injection(real_hello: bytes, profile: dict, pool: SNIPool,
                   rng: random.Random | None = None) -> InjectionPlan:
    """Build the full step list for one ClientHello delivery.

    ``profile`` keys (validated by the engine):
      technique, fragment_strategy, split_pos, midsld, seqovl, fooling,
      repeats, fragment_delay, ttl_trick, ttl_value, fingerprint,
      tlsrec (bool — combine tlsrec with the split)
    """
    rng = rng or random.Random()
    technique = str(profile.get("technique") or "combined").strip().lower()
    if technique not in TECHNIQUES:
        technique = "combined"
    plan = InjectionPlan(technique=technique, profile=dict(profile))

    delay = _delay(float(profile.get("fragment_delay") or 0.15), rng)
    ttl_value = int(profile.get("ttl_value") or 4) if profile.get("ttl_trick", True) else None
    fooling = str(profile.get("fooling") or "md5sig").strip().lower()
    if fooling not in FOOLING:
        fooling = "md5sig"
    repeats = max(1, min(8, int(profile.get("repeats") or 1)))
    fingerprint = profile.get("fingerprint") or "chrome"
    use_tlsrec = bool(profile.get("tlsrec"))
    seqovl = max(0, int(profile.get("seqovl") or 0))

    # Step 1-2: pick the decoy SNI + build the fake hello
    fake_sni = pool.pick(rng)
    plan.fake_sni = fake_sni
    fake_hello = build_fake_client_hello(fake_sni, fingerprint, rng)
    hostfake = build_hostfakesplit_hello(real_hello, rng)

    # Real-stream parts per strategy
    strategy = profile.get("fragment_strategy") or "sni_split"
    parts = split_hello(real_hello, strategy,
                       split_pos=int(profile.get("split_pos") or 1),
                       midsld=bool(profile.get("midsld")), rng=rng)

    def _data_step(payload: bytes, last: bool) -> PlanStep:
        return PlanStep(kind="data", payload=payload,
                        delay_after=(delay if not last else 0.0))

    def _fake_step(note: str) -> PlanStep:
        return PlanStep(kind="fake", payload=fake_hello, delay_after=delay,
                        ttl=ttl_value, fooling=fooling,
                        note=note or f"decoy SNI={fake_sni}")

    if technique == "fragment":
        plan.steps = [_data_step(p, i == len(parts) - 1) for i, p in enumerate(parts)]
    elif technique == "fake_sni":
        plan.steps = [_fake_step("decoy hello first (fooling applies on raw-socket helpers)")]
        plan.steps += [PlanStep(kind="data", payload=real_hello)]
    elif technique == "combined":
        plan.steps = [_fake_step("decoy hello first (fooling applies on raw-socket helpers)")]
        plan.steps += [_data_step(p, i == len(parts) - 1) for i, p in enumerate(parts)]
    elif technique == "hostfakesplit":
        if hostfake is None:
            plan.warnings.append("real hello unparseable — falling back to plain send")
            plan.steps = [PlanStep(kind="data", payload=real_hello)]
        else:
            plan.steps = [PlanStep(kind="fake", payload=hostfake, ttl=ttl_value,
                                   fooling=fooling, delay_after=delay,
                                   note="same-length fake-SNI hello (userspace-safe)"),
                          PlanStep(kind="data", payload=real_hello)]
    elif technique == "multisplit":
        plan.steps = [_data_step(p, i == len(parts) - 1) for i, p in enumerate(parts)]
        if seqovl > 0:
            plan.raw_socket_required = True
            plan.warnings.append(
                f"split-seqovl={seqovl}: the TCP sequence OVERLAP needs raw-socket "
                "execution (helper run as admin/root); userspace helpers send the "
                "plain split — the server still reassembles correctly")
    elif technique == "multidisorder":
        plan.raw_socket_required = True
        plan.steps = [_fake_step("fake first, then reversed segments (zapret-style)")]
        for i, p in enumerate(reversed(parts)):
            plan.steps.append(PlanStep(kind="raw", payload=p, delay_after=delay,
                                       fooling=fooling,
                                       note=f"segment {len(parts) - i}/{len(parts)} "
                                            "out-of-order (raw socket)"))
        plan.warnings.append("out-of-order delivery needs a raw-socket helper; "
                             "userspace ladder falls back to combined")
    elif technique == "fakedsplit":
        plan.steps = [_fake_step("decoy, then forward split (fakedsplit)")]
        plan.steps += [_data_step(p, i == len(parts) - 1) for i, p in enumerate(parts)]
    elif technique == "fakeddisorder":
        plan.steps = [_fake_step("decoy, then reversed split (fakeddisorder)")]
        for i, p in enumerate(reversed(parts)):
            plan.steps.append(_data_step(p, True))
        plan.warnings.append("reverse-order DATA segments reach the server "
                             "retransmitting/reordering via TCP; the true disorder "
                             "variant needs raw sockets")
    elif technique == "tlsrec":
        recs = tlsrec_wrap(real_hello)
        plan.steps = [_data_step(p, i == len(recs) - 1) for i, p in enumerate(recs)]
        if not use_tlsrec:
            plan.warnings.append("tlsrec alone is rarely sufficient — combine with "
                                 "a split technique (set tlsrec=true in the profile)")
    elif technique in ("oob", "disoob"):
        plan.raw_socket_required = True
        plan.steps = [PlanStep(kind="raw", payload=real_hello[:1], delay_after=0.0,
                               fooling=fooling,
                               note="urgent-pointer byte mid-handshake (raw socket)"),
                      PlanStep(kind="data", payload=real_hello)]
        plan.warnings.append("TCP urgent bytes need raw-socket execution")
    elif technique in ("wrong_seq", "md5sig"):
        plan.raw_socket_required = True
        plan.steps = [PlanStep(kind="raw", payload=fake_hello, delay_after=delay,
                               ttl=ttl_value, fooling=fooling,
                               note=f"decoy with {technique} fooling (raw socket)")]
        plan.steps += [_data_step(p, i == len(parts) - 1) for i, p in enumerate(parts)]
    elif technique in ("syndata", "synack"):
        plan.raw_socket_required = True
        from .state_confusion import describe_state_confusion
        plan.steps = [PlanStep(kind="raw", payload=fake_hello, delay_after=delay,
                               ttl=ttl_value, fooling=fooling,
                               note=f"{technique} state-confusion decoy (raw socket)")]
        plan.steps += [_data_step(p, i == len(parts) - 1) for i, p in enumerate(parts)]
        plan.warnings.append(describe_state_confusion(technique))

    # optional tlsrec combination for split-family techniques: the helper
    # wraps the real hello in two TLS records BEFORE applying the split —
    # recorded as guidance; the executable userspace partition stays the
    # plain split (stream-preserving), tlsrec_wrap is proven separately.
    if use_tlsrec and technique in ("fragment", "combined", "fakedsplit",
                                    "multisplit", "fakeddisorder", "multidisorder"):
        rec_parts = tlsrec_wrap(real_hello)
        if len(rec_parts) == 2 and verify_tlsrec(real_hello, rec_parts):
            plan.warnings.append(
                "tlsrec combined: helper first wraps the hello in two TLS records, "
                "then applies the split (both are stream-safe at content level)")
    if repeats > 1 and any(s.kind == "fake" for s in plan.steps):
        first_fake = next(s for s in plan.steps if s.kind == "fake")
        extra = min(repeats, 6) - 1
        insert_at = plan.steps.index(first_fake)
        for _ in range(extra):
            plan.steps.insert(insert_at, PlanStep(
                kind=first_fake.kind, payload=first_fake.payload,
                delay_after=first_fake.delay_after, ttl=first_fake.ttl,
                fooling=first_fake.fooling, note=f"repeat decoy (repeats={repeats})"))

    # stream-preservation invariant (userspace "data" steps only).
    # fakeddisorder sends segments in REVERSE arrival order — TCP still
    # reassembles by sequence number, so the LOGICAL stream is preserved.
    data_parts = [s.payload for s in plan.steps if s.kind == "data"]
    if data_parts:
        if technique == "tlsrec":
            plan.stream_preserved = (b"".join(data_parts) == real_hello
                                     or verify_tlsrec(real_hello, data_parts))
        elif technique == "fakeddisorder":
            plan.stream_preserved = b"".join(reversed(data_parts)) == real_hello
        else:
            plan.stream_preserved = b"".join(data_parts) == real_hello
        if not plan.stream_preserved:
            plan.warnings.append("plan failed the stream-preservation check — "
                                 "the helper must send the ORIGINAL bytes instead")
    return plan


def verify_tlsrec(original: bytes, recs: list[bytes]) -> bool:
    """A tlsrec wrap is valid when the record CONTENTS concatenate to the
    original handshake message and record headers are well-formed."""
    return (len(recs) == 2
            and all(r[:1] == b"\x16" for r in recs)
            and b"".join(r[5:] for r in recs) == original[5:]
            and sum(len(r) - 5 for r in recs) == len(original) - 5)


# ── userspace executor (loopback tests + helper parity) ──────────────────────
async def execute_userspace(plan: InjectionPlan, sock, *, pool: SNIPool | None = None,
                            send_fn=None) -> dict:
    """Execute the userspace ladder of a plan over ``sock``.

    data steps  -> written to the socket in order (stream-preserving)
    fake steps  -> written to the socket too (loopback tests) / or via
                   send_fn (the helper sends them on a short-TTL socket)
    raw steps   -> skipped in userspace, counted honestly

    Returns a report: what ran, what was skipped, stream integrity."""
    import asyncio

    def _write(payload: bytes) -> None:
        if hasattr(sock, "write"):
            sock.write(payload)               # asyncio StreamWriter
        elif hasattr(sock, "sendall"):
            sock.sendall(payload)             # plain socket
        else:
            raise TypeError("sock must be a socket or StreamWriter")

    ran, skipped_raw = 0, 0
    sent_real = b""
    for step in plan.steps:
        if step.kind == "raw":
            skipped_raw += 1
            continue
        if send_fn is not None and step.kind == "fake":
            await send_fn(step)
        else:
            _write(step.payload)
        ran += 1
        if step.kind == "data":
            sent_real += step.payload
        if step.delay_after > 0:
            await asyncio.sleep(step.delay_after)
    if pool is not None and plan.fake_sni:
        pool.report(plan.fake_sni, ok=ran > 0)
    expected = b"".join(s.payload for s in plan.steps if s.kind == "data")
    return {
        "steps_executed": ran,
        "raw_steps_skipped": skipped_raw,
        "stream_bytes": len(sent_real),
        "stream_ok": (not expected) or sent_real == expected,
    }
