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
    # Morph ships ON: its default profile ("baseline": chunk 0 / delay 0) is a
    # byte-exact passthrough, so it costs nothing until profiles are configured
    # — and the engine no longer looks "broken" out of the box.
    coalesce_on: bool = True
    morph_on: bool = True
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
    snispoof_on: bool = True
    reality_on: bool = True

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

    # SNI Spoofing (client-side bypass profile generator + helper)
    sni_method: str = "combined"                 # SNI_METHOD: fragment|fake_sni|combined
    sni_fragment_strategy: str = "sni_split"     # SNI_FRAGMENT_STRATEGY
    sni_fragment_delay: float = 0.1               # SNI_FRAGMENT_DELAY (seconds)
    sni_ttl_trick: bool = True                    # SNI_TTL_TRICK
    sni_ttl_value: int = 1                        # SNI_TTL_VALUE (1-8)
    sni_fake_sni: str = "www.microsoft.com"       # SNI_FAKE_SNI
    sni_pool: list[str] = field(default_factory=list)   # SNI_POOL (csv)
    sni_listen_port: int = 40443                   # SNI_LISTEN_PORT (helper default)
    sni_max_conns: int = 256                      # EMUNEL_SNI_MAX_CONNS (helper bound)

    # REALITY (keypair + config generator + optional pinned runtime)
    reality_target: str = "blubank.com:443"       # REALITY_TARGET
    reality_xhttp_target: str = "divar.ir:443"   # XHTTP_REALITY_TARGET
    reality_server_names: list[str] = field(default_factory=list)  # REALITY_SERVER_NAMES
    reality_fingerprint: str = "chrome"          # REALITY_FINGERPRINT
    reality_short_ids: list[str] = field(default_factory=list)      # REALITY_SHORT_IDS
    reality_spider_x: str = "/"                   # REALITY_SPIDER_X
    reality_private_key: str = ""                # REALITY_PRIVATE_KEY (b64 x25519)
    reality_public_key: str = ""                  # REALITY_PUBLIC_KEY
    reality_listen_host: str = "0.0.0.0"          # EMUNEL_REALITY_LISTEN_HOST
    reality_listen_port: int = 8443               # EMUNEL_REALITY_LISTEN_PORT
    reality_public_port: int = 0                  # EMUNEL_REALITY_PUBLIC_PORT (0 = same)
    reality_uuid: str = ""                        # REALITY_UUID (client uuid)
    reality_flow: str = "xtls-rprx-vision"        # REALITY_FLOW
    xray_binary: str = ""                          # EMUNEL_XRAY_BINARY (pinned runtime)
    xray_sha256: str = ""                          # EMUNEL_XRAY_SHA256

    # ── Evolution engines (Chaos / DpiMesh / Genetic / Synergy) — ALL OFF ──
    # Operator spec: every one of these flags defaults to false, so a
    # deployment that never sets them behaves byte-identically to before.
    chaos_on: bool = False                        # CHAOS_PROTOCOL_ENABLED
    mesh_on: bool = False                         # DPI_MESH_ENABLED
    genetic_on: bool = False                      # GENETIC_ENGINE_ENABLED
    synergy_on: bool = False                      # SYNERGY_ENABLED

    chaos_secret: str = "change_me_please"        # CHAOS_SECRET
    chaos_tick_ms: int = 30_000                    # CHAOS_TICK_MS (30-90s)
    chaos_max_sessions: int = 1024                 # CHAOS_MAX_SESSIONS

    mesh_salt: str = "change_me_please"            # MESH_SALT
    mesh_aggregate_interval_sec: int = 300         # MESH_AGGREGATE_INTERVAL_SEC
    mesh_retention_hours: int = 24                 # MESH_RETENTION_HOURS
    mesh_max_db_mb: int = 10                       # MESH_MAX_DB_MB

    genetic_population_size: int = 20              # GENETIC_POPULATION_SIZE
    genetic_mutation_rate: float = 0.05            # GENETIC_MUTATION_RATE
    genetic_evolve_interval_sec: int = 21_600      # GENETIC_EVOLVE_INTERVAL_SEC (6h)
    genetic_max_db_mb: int = 1                      # GENETIC_MAX_DB_MB

    synergy_loop_sec: int = 300                     # SYNERGY_LOOP_SEC

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
                   "SNIRotation,DomainFronting,PortHopping,SNISpoof,Reality,"
                   "Chaos,Mesh,Genetic,Synergy")

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
    "SNISpoof": "EMUNEL_ENGINE_SNI_SPOOF_ENABLED",
    "Reality": "EMUNEL_ENGINE_REALITY_ENABLED",
    # Evolution engines — the operator spec's flag names, verbatim
    "Chaos": "CHAOS_PROTOCOL_ENABLED",
    "Mesh": "DPI_MESH_ENABLED",
    "Genetic": "GENETIC_ENGINE_ENABLED",
    "Synergy": "SYNERGY_ENABLED",
}

DEFAULT_SNI_POOL = "cdnjs.cloudflare.com,www.hcaptcha.com,auth.vercel.com,www.google.com"
DEFAULT_REALITY_SHORT_IDS = ",0123456789abcdef"


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
        morph_on=_bool("EMUNEL_ENGINE_MORPH_ENABLED", True),
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
        snispoof_on=_bool("EMUNEL_ENGINE_SNI_SPOOF_ENABLED", True),
        reality_on=_bool("EMUNEL_ENGINE_REALITY_ENABLED", True),

        sni_method=(_str("SNI_METHOD", "combined").strip().lower() or "combined"),
        sni_fragment_strategy=(_str("SNI_FRAGMENT_STRATEGY", "sni_split").strip().lower() or "sni_split"),
        sni_fragment_delay=min(2.0, max(0.0, _float("SNI_FRAGMENT_DELAY", 0.1))),
        sni_ttl_trick=_bool("SNI_TTL_TRICK", True),
        sni_ttl_value=min(8, max(1, _int("SNI_TTL_VALUE", 1))),
        sni_fake_sni=_str("SNI_FAKE_SNI", "www.microsoft.com").strip() or "www.microsoft.com",
        sni_pool=_csv("SNI_POOL", DEFAULT_SNI_POOL),
        sni_listen_port=_int("SNI_LISTEN_PORT", 40443),
        sni_max_conns=_int("EMUNEL_SNI_MAX_CONNS", 256),

        reality_target=_str("REALITY_TARGET", "blubank.com:443").strip() or "blubank.com:443",
        reality_xhttp_target=_str("XHTTP_REALITY_TARGET", "divar.ir:443").strip() or "divar.ir:443",
        reality_server_names=_csv("REALITY_SERVER_NAMES", ""),
        reality_fingerprint=_str("REALITY_FINGERPRINT", "chrome").strip().lower() or "chrome",
        reality_short_ids=_csv("REALITY_SHORT_IDS", DEFAULT_REALITY_SHORT_IDS),
        reality_spider_x=_str("REALITY_SPIDER_X", "/") or "/",
        reality_private_key=_str("REALITY_PRIVATE_KEY", "").strip(),
        reality_public_key=_str("REALITY_PUBLIC_KEY", "").strip(),
        reality_listen_host=_str("EMUNEL_REALITY_LISTEN_HOST", "0.0.0.0").strip() or "0.0.0.0",
        reality_listen_port=_int("EMUNEL_REALITY_LISTEN_PORT", 8443),
        reality_public_port=_int("EMUNEL_REALITY_PUBLIC_PORT", 0),
        reality_uuid=_str("REALITY_UUID", "").strip(),
        reality_flow=_str("REALITY_FLOW", "xtls-rprx-vision").strip() or "xtls-rprx-vision",
        xray_binary=_str("EMUNEL_XRAY_BINARY", "").strip(),
        xray_sha256=_str("EMUNEL_XRAY_SHA256", "").strip(),

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

        chaos_on=_bool("CHAOS_PROTOCOL_ENABLED", False),
        mesh_on=_bool("DPI_MESH_ENABLED", False),
        genetic_on=_bool("GENETIC_ENGINE_ENABLED", False),
        synergy_on=_bool("SYNERGY_ENABLED", False),

        chaos_secret=_str("CHAOS_SECRET", "change_me_please") or "change_me_please",
        chaos_tick_ms=max(100, _int("CHAOS_TICK_MS", 30_000)),
        chaos_max_sessions=max(8, _int("CHAOS_MAX_SESSIONS", 1024)),

        mesh_salt=_str("MESH_SALT", "change_me_please") or "change_me_please",
        mesh_aggregate_interval_sec=max(5, _int("MESH_AGGREGATE_INTERVAL_SEC", 300)),
        mesh_retention_hours=max(1, _int("MESH_RETENTION_HOURS", 24)),
        mesh_max_db_mb=max(1, _int("MESH_MAX_DB_MB", 10)),

        genetic_population_size=min(64, max(4, _int("GENETIC_POPULATION_SIZE", 20))),
        genetic_mutation_rate=min(1.0, max(0.0, _float("GENETIC_MUTATION_RATE", 0.05))),
        genetic_evolve_interval_sec=max(1, _int("GENETIC_EVOLVE_INTERVAL_SEC", 21_600)),
        genetic_max_db_mb=max(1, _int("GENETIC_MAX_DB_MB", 1)),

        synergy_loop_sec=max(1, _int("SYNERGY_LOOP_SEC", 300)),

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
