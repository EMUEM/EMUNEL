"""Engine environment configuration.

Every tunable the engines use is read here from environment variables —
engine code never hardcodes numbers. The ``env`` singleton is the parsed
snapshot taken at manager construction time (re-parsed per process).

Variable naming follows the operator's spec for the per-engine parameters
(MAX_COALESCE_SIZE, COALESCE_TIMEOUT_MS, COMPRESSION_LEVEL, ...) and the
EMUNEL_ENGINE_* prefix for enable flags and engine-system settings.
PIPELINE_ORDER is honoured as-is (EMUNEL_PIPELINE_ORDER wins when both are
set, so the EMUNEL namespace stays unambiguous on shared platforms).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return raw if raw is not None else default


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _json(name: str, default: dict) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else dict(default)
    except ValueError:
        return dict(default)


# Default Iranian domain set for split tunneling (public knowledge; the
# operator can extend/replace via EMUNEL_SPLIT_DOMAINS). *.ir is covered by
# the ".ir" suffix rule; the list below adds the popular non-.ir services
# that are hosted/served for Iran and should go direct.
DEFAULT_SPLIT_DOMAINS = (
    ".ir,aparat.com,digikala.com,digikala.net,digikala.org,"
    "snapp.ir,snappfood.ir,snapp.taxi,cafebazaar.org,cafebazaar.ir,"
    "divar.ir,irancell.ir,mci.ir,mtn.ir,my.irancell.ir,"
    "shatel.ir,parsonline.com,hiweb.ir,parsonline.net,"
    "bankmelli-ir.com,bmi.ir,banksaderat.net,banksepah.ir,tejaratbank.ir,"
    "bpi.ir,postbank.ir,ensani.ir,samanbank.ir,sbank.ir,pasargadbank.ir,"
    "melalibank.ir,kosarbank.ir,bank-kosar.com,resalatbank.ir,"
    "shaparak.ir,shaparak.com,sep.ir,rahkar soft.io,"
    "iran.ir,irna.ir,isna.ir,mehrnews.com,tasnimnews.com,farsnews.com,"
    "khabaronline.ir,yjc.ir,hamshahrionline.ir,varzesh3.com,aparat.ir"
)

# Major Iranian ISP/AS IPv4 aggregates (public BGP announcements, trimmed to
# the big blocks) — used by the Morphing engine to pick per-ISP profiles and
# by the congestion engine for reporting. Operator-extendable via
# EMUNEL_MORPH_ISP_PREFIXES=prefix=isp,prefix=isp,...
DEFAULT_ISP_PREFIXES = (
    "2.144.0.0/13=mci,2.176.0.0/12=mci,5.106.0.0/16=mci,5.112.0.0/16=mci,"
    "31.7.64.0/18=mci,46.209.0.0/16=mci,78.38.0.0/16=mci,81.12.0.0/17=mci,"
    "85.133.128.0/18=mci,188.121.96.0/19=mci,217.218.0.0/16=mci,"
    "2.148.0.0/14=irancell,5.125.0.0/16=irancell,46.148.32.0/19=irancell,"
    "78.47.0.0/16=other,79.175.128.0/18=irancell,82.99.192.0/18=irancell,"
    "85.198.0.0/16=irancell,91.98.0.0/15=irancell,151.240.0.0/12=irancell,"
    "178.131.0.0/16=irancell,185.105.236.0/22=irancell,"
    "31.56.0.0/13=shatel,62.220.96.0/19=shatel,81.31.160.0/19=shatel,"
    "89.144.128.0/18=shatel,94.182.0.0/15=shatel,151.232.0.0/13=shatel,"
    "5.102.32.0/20=parsonline,5.160.0.0/16=parsonline,188.121.128.0/18=parsonline,"
    "79.127.0.0/17=fanava,81.28.64.0/18=fanava,"
    "213.207.192.0/18=parsonline,217.24.144.0/20=parsonline,"
    "37.156.0.0/16=rahkar,87.247.160.0/19=rahkar,87.251.128.0/19=rahkar"
)


@dataclass
class EngineEnv:
    # ── engine system ──────────────────────────────────────────────────────
    enabled: bool = True                      # EMUNEL_ENGINES_ENABLED
    pipeline_order: list[str] = field(default_factory=list)
    data_dir: str = ""                         # resolved at first use
    bypass_errors: int = 3
    bypass_cooldown_sec: float = 60.0
    status_cache_sec: float = 15.0
    # resource guards (userspace caps; see engines/README.md)
    max_ops_per_sec: int = 2000
    max_buffer_bytes: int = 262144
    max_http_buffer_bytes: int = 8 * 1024 * 1024

    # ── per-engine enable flags ───────────────────────────────────────────
    coalesce_on: bool = True
    morph_on: bool = False
    compress_on: bool = False
    preconnect_on: bool = True
    fec_on: bool = False
    congestion_on: bool = True
    session_on: bool = True
    fake_on: bool = True
    split_on: bool = True
    sni_on: bool = False
    fronting_on: bool = False
    porthop_on: bool = False

    # ── Coalescing ─────────────────────────────────────────────────────────
    coalesce_max_size: int = 16384             # MAX_COALESCE_SIZE
    coalesce_timeout_ms: int = 8              # COALESCE_TIMEOUT_MS
    coalesce_max_buffer: int = 262144

    # ── Morphing ───────────────────────────────────────────────────────────
    morph_default_chunk: int = 16384
    morph_default_delay_ms: int = 0
    morph_padding_percent: int = 0
    morph_bandit_alpha: float = 0.35
    morph_isp_prefixes: dict = field(default_factory=dict)
    morph_profiles: dict = field(default_factory=dict)
    morph_selfplay: bool = False
    morph_selfplay_url: str = ""
    morph_selfplay_interval_sec: int = 300
    morph_success_bytes: int = 4096           # bytes relayed == "worked"

    # ── Compression ───────────────────────────────────────────────────────
    compress_level: int = 6                   # COMPRESSION_LEVEL
    compress_min_saving: int = 5               # MIN_SAVING_PERCENT
    compress_algos: list[str] = field(default_factory=list)
    compress_sample: int = 65536

    # ── Pre-connect ───────────────────────────────────────────────────────
    preconnect_pool_size: int = 2             # PRECONNECT_POOL_SIZE
    preconnect_ttl_sec: int = 30              # PRECONNECT_TTL_SEC
    preconnect_max_hosts: int = 32
    preconnect_connect_timeout: float = 3.0

    # ── FEC ───────────────────────────────────────────────────────────────
    fec_ratio: float = 0.25                    # FEC_RATIO
    fec_block_size: int = 1408                 # FEC_BLOCK_SIZE

    # ── Congestion ────────────────────────────────────────────────────────
    cc_default: str = "bbr"                    # CC_DEFAULT (advisory label)
    cc_switch_threshold: float = 2.0           # CC_SWITCH_THRESHOLD
    cc_rtt_window_sec: int = 30
    cc_sock_buf: int = 262144
    cc_quickack: bool = True

    # ── Session resumption ────────────────────────────────────────────────
    session_cache_size: int = 256              # SESSION_CACHE_SIZE
    session_ttl_hours: int = 24                # SESSION_TTL_HOURS

    # ── Fake handshake ────────────────────────────────────────────────────
    fake_server_type: str = "nginx"           # EMUNEL_FAKE_SERVER_TYPE

    # ── Domain fronting / SNI rotation / port hopping ─────────────────────
    fronting_sni_domain: str = ""
    fronting_real_host: str = ""
    sni_domains: list[str] = field(default_factory=list)
    porthop_ports: list[int] = field(default_factory=list)

    # ── Split tunneling ────────────────────────────────────────────────────
    split_domains: list[str] = field(default_factory=list)
    split_ip_cidrs: list[str] = field(default_factory=list)

    # ── runtime info (not env) ─────────────────────────────────────────────
    host: str = "console"                      # which process we run in
    explicit_off: set = field(default_factory=set)   # env kill-switches (EMUNEL_ENGINE_*_ENABLED=0)

    @property
    def coalesce_timeout_s(self) -> float:
        return self.coalesce_timeout_ms / 1000.0


# Every engine participates in the pipeline by default; each deactivates
# itself with a visible reason (env flag, preconditions, host). The order
# below is the execution order for the frame pipeline.
DEFAULT_PIPELINE = ("Coalesce,Morph,Compress,PreConnect,FEC,Congestion,"
                   "SessionResumption,FakeHandshake,SplitTunnel,"
                   "SNIRotation,DomainFronting,PortHopping")

# engine NAME -> enable-flag env var (hot toggles may override a missing
# default, but an explicit =0 in the environment is a hard kill-switch)
ENGINE_FLAG_VARS = {
    "Coalesce": "EMUNEL_ENGINE_COALESCE_ENABLED",
    "Morph": "EMUNEL_ENGINE_MORPH_ENABLED",
    "Compress": "EMUNEL_ENGINE_COMPRESS_ENABLED",
    "PreConnect": "EMUNEL_ENGINE_PRECONNECT_ENABLED",
    "FEC": "EMUNEL_ENGINE_FEC_ENABLED",
    "Congestion": "EMUNEL_ENGINE_CONGESTION_ENABLED",
    "SessionResumption": "EMUNEL_ENGINE_SESSION_RESUMPTION_ENABLED",
    "FakeHandshake": "EMUNEL_ENGINE_FAKE_HANDSHAKE_ENABLED",
    "SplitTunnel": "EMUNEL_ENGINE_SPLIT_TUNNEL_ENABLED",
    "SNIRotation": "EMUNEL_ENGINE_SNI_ROTATION_ENABLED",
    "DomainFronting": "EMUNEL_ENGINE_FRONTING_ENABLED",
    "PortHopping": "EMUNEL_ENGINE_PORT_HOPPING_ENABLED",
}


def parse_env(host: str = "console") -> EngineEnv:
    """Snapshot all engine environment variables into an EngineEnv."""
    pipeline_raw = os.environ.get("EMUNEL_PIPELINE_ORDER") or os.environ.get("PIPELINE_ORDER") or ""
    cfg = EngineEnv(
        enabled=_bool("EMUNEL_ENGINES_ENABLED", True),
        pipeline_order=[p.strip() for p in pipeline_raw.split(",") if p.strip()] or
                       [p.strip() for p in DEFAULT_PIPELINE.split(",")],
        bypass_errors=_int("EMUNEL_ENGINE_BYPASS_ERRORS", 3),
        bypass_cooldown_sec=_float("EMUNEL_ENGINE_BYPASS_COOLDOWN_SEC", 60.0),
        status_cache_sec=_float("EMUNEL_ENGINE_STATUS_CACHE_SEC", 15.0),
        max_ops_per_sec=_int("EMUNEL_ENGINE_MAX_OPS_PER_SEC", 2000),
        max_buffer_bytes=_int("EMUNEL_ENGINE_MAX_BUFFER_BYTES", 262144),
        max_http_buffer_bytes=_int("EMUNEL_ENGINE_HTTP_MAX_BUFFER_BYTES", 8 * 1024 * 1024),

        coalesce_on=_bool("EMUNEL_ENGINE_COALESCE_ENABLED", True),
        morph_on=_bool("EMUNEL_ENGINE_MORPH_ENABLED", False),
        compress_on=_bool("EMUNEL_ENGINE_COMPRESS_ENABLED", False),
        preconnect_on=_bool("EMUNEL_ENGINE_PRECONNECT_ENABLED", True),
        fec_on=_bool("EMUNEL_ENGINE_FEC_ENABLED", False),
        congestion_on=_bool("EMUNEL_ENGINE_CONGESTION_ENABLED", True),
        session_on=_bool("EMUNEL_ENGINE_SESSION_RESUMPTION_ENABLED", True),
        fake_on=_bool("EMUNEL_ENGINE_FAKE_HANDSHAKE_ENABLED", True),
        split_on=_bool("EMUNEL_ENGINE_SPLIT_TUNNEL_ENABLED", True),
        sni_on=_bool("EMUNEL_ENGINE_SNI_ROTATION_ENABLED", False),
        fronting_on=_bool("EMUNEL_ENGINE_FRONTING_ENABLED", False),
        porthop_on=_bool("EMUNEL_ENGINE_PORT_HOPPING_ENABLED", False),

        coalesce_max_size=_int("MAX_COALESCE_SIZE", 16384),
        coalesce_timeout_ms=_int("COALESCE_TIMEOUT_MS", 8),
        coalesce_max_buffer=_int("EMUNEL_ENGINE_COALESCE_MAX_BUFFER", 262144),

        morph_default_chunk=_int("EMUNEL_MORPH_DEFAULT_CHUNK", 16384),
        morph_default_delay_ms=_int("EMUNEL_MORPH_DEFAULT_INTER_DELAY_MS", 0),
        morph_padding_percent=_int("EMUNEL_MORPH_PADDING_PERCENT", 0),
        morph_bandit_alpha=_float("EMUNEL_MORPH_BANDIT_ALPHA", 0.35),
        morph_isp_prefixes=_parse_isp_map(
            os.environ.get("EMUNEL_MORPH_ISP_PREFIXES", DEFAULT_ISP_PREFIXES)),
        morph_profiles=_json("EMUNEL_MORPH_PROFILES", {}),
        morph_selfplay=_bool("EMUNEL_MORPH_SELFPLAY", False),
        morph_selfplay_url=_str("EMUNEL_MORPH_SELFPLAY_URL"),
        morph_selfplay_interval_sec=_int("EMUNEL_MORPH_SELFPLAY_INTERVAL_SEC", 300),
        morph_success_bytes=_int("EMUNEL_MORPH_SUCCESS_BYTES", 4096),

        compress_level=_int("COMPRESSION_LEVEL", 6),
        compress_min_saving=_int("MIN_SAVING_PERCENT", 5),
        compress_algos=_csv("EMUNEL_COMPRESS_ALGOS", "zlib"),
        compress_sample=_int("EMUNEL_ENGINE_COMPRESS_MAX_SAMPLE", 65536),

        preconnect_pool_size=_int("PRECONNECT_POOL_SIZE", 2),
        preconnect_ttl_sec=_int("PRECONNECT_TTL_SEC", 30),
        preconnect_max_hosts=_int("EMUNEL_PRECONNECT_MAX_HOSTS", 32),
        preconnect_connect_timeout=_float("EMUNEL_PRECONNECT_CONNECT_TIMEOUT", 3.0),

        fec_ratio=_float("FEC_RATIO", 0.25),
        fec_block_size=_int("FEC_BLOCK_SIZE", 1408),

        cc_default=_str("CC_DEFAULT", "bbr"),
        cc_switch_threshold=_float("CC_SWITCH_THRESHOLD", 2.0),
        cc_rtt_window_sec=_int("EMUNEL_CC_RTT_WINDOW_SEC", 30),
        cc_sock_buf=_int("EMUNEL_CC_SOCK_BUF", 262144),
        cc_quickack=_bool("EMUNEL_CC_QUICKACK", True),

        session_cache_size=_int("SESSION_CACHE_SIZE", 256),
        session_ttl_hours=_int("SESSION_TTL_HOURS", 24),

        fake_server_type=_str("EMUNEL_FAKE_SERVER_TYPE", "nginx"),

        fronting_sni_domain=_str("EMUNEL_FRONTING_SNI_DOMAIN"),
        fronting_real_host=_str("EMUNEL_FRONTING_REAL_HOST"),
        sni_domains=_csv("EMUNEL_SNI_DOMAINS", ""),
        porthop_ports=[int(p) for p in _csv("EMUNEL_PORT_HOPPING_PORTS", "443") if p.isdigit()],

        split_domains=_csv("EMUNEL_SPLIT_DOMAINS", DEFAULT_SPLIT_DOMAINS),
        split_ip_cidrs=_csv("EMUNEL_SPLIT_IP_CIDRS", ""),

        host=host,
    )
    # explicit env kill-switches: EMUNEL_ENGINE_<NAME>_ENABLED=0 set by the
    # operator. Missing/unset flags are just defaults (panel can toggle).
    cfg.explicit_off = {
        name for name, var in ENGINE_FLAG_VARS.items()
        if os.environ.get(var, "").strip().lower() in ("0", "false", "no", "off")
    }
    cfg.data_dir = resolve_data_dir()
    return cfg


def _parse_isp_map(raw: str) -> dict:
    """`prefix=isp, prefix=isp` (also accepts JSON {"prefix": "isp"})."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except ValueError:
            return {}
        return {}
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if "=" not in item:
            continue
        prefix, isp = item.split("=", 1)
        prefix, isp = prefix.strip(), isp.strip().lower()
        if prefix and isp:
            out[prefix] = isp
    return out


def resolve_data_dir() -> str:
    """First writable data root, preferring the platform volume mount point.

    Mirrors main.py's resolution order so engines live next to the rest of
    the platform state: /data (Railway volume mount) -> repo .emunel-data ->
    /tmp fallback (ephemeral; a warning is emitted by the state store).
    """
    explicit = os.environ.get("EMUNEL_ENGINE_DATA")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("EMUNEL_WORKER_DATA"):
        # stay on the same volume the worker uses, one level up from /instances
        candidates.append(Path(os.environ["EMUNEL_WORKER_DATA"]).parent / "engines")
    candidates.extend([Path("/data/engines"),
                       Path(__file__).resolve().parents[1] / ".emunel-data" / "engines",
                       Path("/tmp/emunel-engines")])
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-probe"
            probe.write_text("ok")
            probe.unlink()
            return str(cand)
        except OSError:
            continue
    return str(candidates[-1])


# Shared, lazily-parsed snapshot for code paths that run before the manager
# is constructed (e.g. module import checks in tests).
env: EngineEnv | None = None


def get_env(host: str = "console") -> EngineEnv:
    global env
    if env is None or env.host != host:
        env = parse_env(host)
    return env
