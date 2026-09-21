"""REALITY Engine — X25519 keypairs, config generation, optional pinned runtime.

REALITY (XTLS) borrows a real target site's TLS handshake as camouflage:
the CLIENT sends the target's SNI (e.g. blubank.com) and authenticates the
server through an X25519 key exchange embedded in the handshake, while the
SERVER proxies the target's certificate for everyone it does not know.
From the censor's viewpoint the connection looks like ordinary TLS to a
popular domestic site.

What runs where (honest scope):

  * ALWAYS available on Railway: keypair generation (X25519), the full
    inbound (server) and outbound (client) JSON configs for RAW / XHTTP /
    gRPC transports, and an importable vless:// share link.
  * OPTIONAL runtime: when the operator installs an Xray binary and pins
    it (EMUNEL_XRAY_BINARY + EMUNEL_XRAY_SHA256 — the same provenance rule
    the Core's VMess runtime uses, never downloaded by the panel), this
    engine runs one VLESS+REALITY listener inside the container on
    EMUNEL_REALITY_LISTEN_PORT and exposes it through a Railway TCP Proxy.
    Without the binary the engine stays honestly inactive as a RUNTIME
    while config generation keeps working — the Bypass tab never shows a
    dead control.

Iran guidance (from the operator's spec): pick targets the ISP cannot
block wholesale — domestic heavy-traffic sites (blubank.com, divar.ir,
snapp.ir). Avoid google.com / microsoft.com (censor-monitored).

Key format: X25519 raw 32 bytes, standard base64 (exactly what
`xray x25519` prints); the public key is always derived from the private
one, so env-supplied pairs are verified on boot.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from ..base import Engine

TRANSPORTS = ("raw", "xhttp", "grpc")
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "randomized")


# ---------------------------------------------------------------------------
# X25519 (via the already-required `cryptography` package)
# ---------------------------------------------------------------------------
def generate_x25519_keypair() -> tuple[str, str]:
    """(private_b64, public_b64) — Xray-compatible formatting."""
    from cryptography.hazmat.primitives.asymmetric import x25519

    private = x25519.X25519PrivateKey.generate()
    pub = private.public_key().public_bytes_raw()
    raw = private.private_bytes_raw()
    return base64.b64encode(raw).decode(), base64.b64encode(pub).decode()


def derive_x25519_public(private_b64: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import x25519

    raw = base64.b64decode(private_b64, validate=True)
    if len(raw) != 32:
        raise ValueError("REALITY private key must be 32 bytes (base64)")
    private = x25519.X25519PrivateKey.from_private_bytes(raw)
    return base64.b64encode(private.public_key().public_bytes_raw()).decode()


def _valid_b64_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def _parse_target(target: str, default_port: int = 443) -> tuple[str, int]:
    host = (target or "").strip()
    if "://" in host:
        raise ValueError("target must be host:port, not a URL")
    port = default_port
    if ":" in host:
        host, _, port_s = host.rpartition(":")
        if not host or not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
            raise ValueError("target must look like host:port (e.g. blubank.com:443)")
        port = int(port_s)
    if not host or len(host) > 253:
        raise ValueError("target host is missing or too long")
    return host, port


# ---------------------------------------------------------------------------
# Config generation (server inbound / client outbound / share link)
# ---------------------------------------------------------------------------
def build_inbound(cfg_profile: dict, *, listen_host: str, listen_port: int,
                  uuid: str, private_key: str) -> dict:
    """VLESS+REALITY server inbound (runs on the operator's Xray)."""
    transport = cfg_profile.get("transport") or "raw"
    target = cfg_profile.get("target") or "blubank.com:443"
    stream: dict = {
        "network": transport,
        "security": "reality",
        "realitySettings": {
            "show": False,
            "target": target,
            "xver": 0,
            "serverNames": cfg_profile.get("server_names") or [],
            "privateKey": private_key,
            "shortIds": cfg_profile.get("short_ids") or [""],
            "minClientVer": "",
            "maxClientVer": "",
            "maxTimeDiff": 0,
        },
    }
    if transport == "grpc":
        stream["grpcSettings"] = {"serviceName": cfg_profile.get("grpc_service") or "emunel"}
    elif transport == "xhttp":
        stream["xhttpSettings"] = {"path": cfg_profile.get("xhttp_path") or "/reality",
                                   "host": _parse_target(target)[0]}
    else:
        stream["rawSettings"] = {"header": {"type": "none"}}
    return {
        "listen": listen_host,
        "port": listen_port,
        "protocol": "vless",
        "settings": {
            "clients": [{"id": uuid, "flow": cfg_profile.get("flow") or "xtls-rprx-vision"}],
            "decryption": "none",
        },
        "streamSettings": stream,
    }


def build_outbound(cfg_profile: dict, *, address: str, port: int, uuid: str,
                   public_key: str) -> dict:
    """VLESS+REALITY client outbound for the user's proxy client."""
    transport = cfg_profile.get("transport") or "raw"
    target_host = _parse_target(cfg_profile.get("target") or "blubank.com:443")[0]
    short_id = (cfg_profile.get("short_ids") or [""])[-1] or ""
    stream: dict = {
        "network": transport,
        "security": "reality",
        "realitySettings": {
            "serverName": cfg_profile.get("server_name") or target_host,
            "fingerprint": cfg_profile.get("fingerprint") or "chrome",
            "password": public_key,          # xray >= 1.8.4 field name (was publicKey)
            "publicKey": public_key,
            "shortId": short_id,
            "spiderX": cfg_profile.get("spider_x") or "/",
        },
    }
    if transport == "grpc":
        stream["grpcSettings"] = {"serviceName": cfg_profile.get("grpc_service") or "emunel"}
    elif transport == "xhttp":
        stream["xhttpSettings"] = {"path": cfg_profile.get("xhttp_path") or "/reality",
                                   "host": target_host, "mode": "auto"}
    else:
        stream["rawSettings"] = {"header": {"type": "none"}}
    return {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": address,
                "port": port,
                "users": [{"id": uuid, "flow": cfg_profile.get("flow") or "xtls-rprx-vision",
                           "encryption": "none"}],
            }]
        },
        "streamSettings": stream,
    }


def build_share_link(cfg_profile: dict, *, address: str, port: int, uuid: str,
                     public_key: str, remark: str = "EMUNEL-Reality") -> str:
    from urllib.parse import quote

    transport = cfg_profile.get("transport") or "raw"
    target_host = _parse_target(cfg_profile.get("target") or "blubank.com:443")[0]
    short_id = (cfg_profile.get("short_ids") or [""])[-1] or ""
    params = {
        "encryption": "none",
        "security": "reality",
        "type": "xhttp" if transport == "xhttp" else transport,
        "host": target_host,
        "path": quote(cfg_profile.get("xhttp_path") or "/reality", safe=""),
        "sni": cfg_profile.get("server_name") or target_host,
        "fp": cfg_profile.get("fingerprint") or "chrome",
        "pbk": public_key,
        "sid": short_id,
        "spx": quote(cfg_profile.get("spider_x") or "/", safe=""),
    }
    if (cfg_profile.get("flow") or "xtls-rprx-vision") and transport == "raw":
        params["flow"] = cfg_profile.get("flow") or "xtls-rprx-vision"
    if transport == "grpc":
        params.pop("path", None)
        params["serviceName"] = cfg_profile.get("grpc_service") or "emunel"
    if transport == "raw":
        params.pop("path", None)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    port_part = f":{port}" if ":" not in address else ""
    return f"vless://{uuid}@{address}{port_part}?{query}#{quote(remark)}"


# Pinned-binary verification — same provenance rule as the Core's VMess
# runtime (never downloaded, never shell-executed, digest-pinned, 0600-ish).
# A binary baked into the image at BUILD time (Dockerfile ARG XRAY_VERSION,
# verified there against its pinned digest) satisfies the same rule: the
# digest file rides next to the binary and is re-checked at every start.
# ---------------------------------------------------------------------------
BAKED_XRAY_PATH = "/opt/xray/xray"


def baked_xray() -> tuple[str, str] | None:
    """(path, sha256) of an Xray baked into the image by the Docker build
    (ARG XRAY_VERSION + digest file), or None when not baked."""
    try:
        path = Path(BAKED_XRAY_PATH)
        if not path.is_absolute() or not path.is_file():
            return None
        digest = path.with_suffix(".sha256").read_text(encoding="utf-8").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return None
        return str(path), digest
    except (OSError, ValueError):
        return None


def verify_xray_binary(binary_path: str, sha256: str) -> str:
    path = Path(binary_path or "")
    if not binary_path or not path.is_absolute() or not path.is_file():
        raise RuntimeError(
            "REALITY runtime requires EMUNEL_XRAY_BINARY (absolute path to an "
            "installed Xray executable) — see docs/RAILWAY.md")
    expected = (sha256 or "").lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RuntimeError("REALITY runtime requires EMUNEL_XRAY_SHA256 from a trusted release")
    if not os.access(path, os.X_OK):
        raise RuntimeError("Xray binary is not executable")
    if path.stat().st_mode & 0o022:
        raise RuntimeError("Xray binary must not be group/world writable")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if not secrets.compare_digest(digest.hexdigest(), expected):
        raise RuntimeError("Xray SHA256 mismatch; refusing execution")
    return str(path)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class RealityEngine(Engine):
    NAME = "Reality"
    TITLE = "REALITY TLS camouflage — keypairs, config generator, optional pinned-Xray runtime"
    HANDLES = frozenset()            # service engine: API/Bypass tab
    HOSTS = frozenset({"console"})

    async def init(self, config: dict) -> None:  # noqa: ARG002
        stored = self.state.get(self.NAME, {}) or {}
        self.profile = self._merge_profile(stored.get("profile") or {})
        # keypair precedence: persisted -> env (verified) -> generate on demand
        self._private_key = stored.get("private_key") or self.cfg.reality_private_key
        self._public_key = stored.get("public_key") or self.cfg.reality_public_key
        self._client_uuid = stored.get("client_uuid") or self.cfg.reality_uuid or ""
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_config: Path | None = None
        self._stderr_task: asyncio.Task | None = None
        if self._private_key and _valid_b64_key(self._private_key):
            derived = derive_x25519_public(self._private_key)
            if self._public_key and self._public_key != derived:
                self.log.warning("REALITY_PUBLIC_KEY does not match the private key — using the derived one")
            self._public_key = derived
            if not stored.get("private_key"):
                self._persist_key(self._private_key, derived)
        elif self._private_key:
            self.log.error("stored REALITY private key is not valid base64-32 — ignoring it")
            self._private_key, self._public_key = "", ""
        self.status.metrics.update({
            "keys_generated": int(stored.get("keys_generated", 0) or 0),
            "configs_generated": int(stored.get("configs_generated", 0) or 0),
            "runtime_restarts": int(stored.get("runtime_restarts", 0) or 0),
            "runtime_running": False,
        })

    async def start(self) -> None:
        if self.runtime_configured():
            await self.runtime_start()
        else:
            self.log.info(
                "generator ready (keypair + inbound/outbound configs); runtime "
                "inactive: set EMUNEL_XRAY_BINARY + EMUNEL_XRAY_SHA256 to run "
                "the VLESS+REALITY listener — see docs/RAILWAY.md")

    async def stop(self) -> None:
        await self.runtime_stop()

    def preconditions(self) -> str | None:
        return None      # generator always can run; runtime reports its own state

    def defaults(self) -> dict:
        return {
            "target": self.profile["target"],
            "xhttp_target": self.profile["xhttp_target"],
            "server_names": self.profile["server_names"],
            "fingerprint": self.profile["fingerprint"],
            "short_ids": self.profile["short_ids"],
            "spider_x": self.profile["spider_x"],
            "listen_port": self.profile["listen_port"],
            "keypair_present": bool(self._public_key),
            "runtime_configured": self.runtime_configured(),
        }

    # ---- profile -------------------------------------------------------------
    def _merge_profile(self, override: dict) -> dict:
        p = {
            "target": self.cfg.reality_target,
            "xhttp_target": self.cfg.reality_xhttp_target,
            "server_names": list(self.cfg.reality_server_names or []),
            "fingerprint": self.cfg.reality_fingerprint if self.cfg.reality_fingerprint in FINGERPRINTS else "chrome",
            "short_ids": list(self.cfg.reality_short_ids or ["", "0123456789abcdef"]),
            "spider_x": self.cfg.reality_spider_x,
            "listen_port": int(self.cfg.reality_listen_port),
        }
        if isinstance(override, dict) and override:
            p.update({k: override[k] for k in p if k in override})
        host = _parse_target(p["target"])[0]
        if not p["server_names"]:
            p["server_names"] = [host, f"www.{host}"] if not host.startswith("www.") else [host, host[4:]]
        if not p["short_ids"]:
            p["short_ids"] = ["", secrets.token_hex(8)]
        return p

    def set_profile(self, body: dict) -> dict:
        update: dict = {}
        if "target" in body:
            update["target"] = f"{_parse_target(str(body.get('target') or ''))[0]}:443"
        if "xhttp_target" in body:
            update["xhttp_target"] = f"{_parse_target(str(body.get('xhttp_target') or ''))[0]}:443"
        if "server_names" in body:
            raw = body.get("server_names")
            if isinstance(raw, str):
                raw = [s.strip() for s in raw.replace("\n", ",").split(",")]
            if not isinstance(raw, list):
                raise ValueError("server_names must be a list of hostnames")
            clean = []
            for item in raw:
                host = str(item or "").strip()
                if not host:
                    continue
                if "." not in host or len(host) > 253 or "://" in host:
                    raise ValueError(f"server_names entry invalid: {host!r}")
                clean.append(host)
            if not clean:
                raise ValueError("server_names cannot be empty")
            update["server_names"] = clean[:32]
        if "fingerprint" in body:
            fp = str(body.get("fingerprint") or "").strip().lower()
            if fp not in FINGERPRINTS:
                raise ValueError(f"fingerprint must be one of {', '.join(FINGERPRINTS)}")
            update["fingerprint"] = fp
        if "short_ids" in body:
            raw = body.get("short_ids")
            if isinstance(raw, str):
                raw = [s.strip() for s in raw.replace("\n", ",").split(",")]
            if not isinstance(raw, list):
                raise ValueError("short_ids must be a list of hex strings")
            clean = []
            for item in raw:
                sid = str(item or "").strip()
                if sid == "":
                    clean.append("")
                    continue
                if len(sid) > 16 or any(c not in "0123456789abcdefABCDEF" for c in sid):
                    raise ValueError(f"short id invalid (max 16 hex chars): {sid!r}")
                clean.append(sid.lower())
            update["short_ids"] = clean[:64] or [""]
        if "spider_x" in body:
            spx = str(body.get("spider_x") or "/").strip()
            if not spx.startswith("/") or len(spx) > 200:
                raise ValueError("spider_x must be a path starting with /")
            update["spider_x"] = spx
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
        self.log.info("profile updated: " + ", ".join(sorted(update)))
        if self.runtime_running():
            asyncio.get_running_loop().create_task(self._soft_restart_runtime())
        return dict(self.profile)

    # ---- keys ---------------------------------------------------------------
    def _persist_key(self, private_b64: str, public_b64: str) -> None:
        stored = self.state.get(self.NAME, {}) or {}
        stored.update({
            "private_key": private_b64,
            "public_key": public_b64,
            "client_uuid": (getattr(self, "_client_uuid", "") or "")
                           or str(secrets.token_hex(16)),
        })
        self.state.set(self.NAME, stored)
        if not self._client_uuid:
            self._client_uuid = stored["client_uuid"]

    def generate_keys(self) -> dict:
        """Fresh X25519 pair; becomes the active pair (persisted, 0600-ish
        through the engine state store). The private key is returned ONCE —
        only the public key stays readable from status afterwards."""
        private, public = generate_x25519_keypair()
        self._private_key, self._public_key = private, public
        self._persist_key(private, public)
        self.status.metrics["keys_generated"] = \
            int(self.status.metrics.get("keys_generated", 0)) + 1
        self.log.info(f"new X25519 keypair generated (public={public[:8]}...)")
        if self.runtime_running():
            asyncio.get_running_loop().create_task(self._soft_restart_runtime())
        return {"private_key": private, "public_key": public,
                "client_uuid": self._client_uuid}

    def active_public_key(self) -> str:
        if not self._public_key:
            private, public = generate_x25519_keypair()
            self._private_key, self._public_key = private, public
            self._persist_key(private, public)
            self.status.metrics["keys_generated"] = \
                int(self.status.metrics.get("keys_generated", 0)) + 1
        return self._public_key

    def active_client_uuid(self) -> str:
        if not self._client_uuid:
            self._client_uuid = str(secrets.token_hex(16))
            stored = self.state.get(self.NAME, {}) or {}
            stored["client_uuid"] = self._client_uuid
            self.state.set(self.NAME, stored)
        return self._client_uuid

    # ---- config generation ----------------------------------------------------
    def generate_configs(self, *, address: str, transport: str = "raw",
                         port: int | None = None) -> dict:
        transport = (transport or "raw").strip().lower()
        if transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}")
        profile = dict(self.profile)
        if transport == "xhttp":
            profile["target"] = profile.get("xhttp_target") or profile["target"]
        profile["transport"] = transport
        public = self.active_public_key()
        uuid = self.active_client_uuid()
        listen_port = int(port or self.profile["listen_port"])
        inbound = build_inbound(profile, listen_host=self.cfg.reality_listen_host,
                                listen_port=listen_port, uuid=uuid,
                                private_key=self._private_key or "")
        outbound = build_outbound(profile, address=address, port=listen_port,
                                  uuid=uuid, public_key=public)
        link = build_share_link(profile, address=address, port=listen_port,
                                uuid=uuid, public_key=public)
        stored = self.state.get(self.NAME, {}) or {}
        stored["configs_generated"] = int(stored.get("configs_generated", 0) or 0) + 1
        self.state.set(self.NAME, stored)
        self.status.metrics["configs_generated"] = \
            int(self.status.metrics.get("configs_generated", 0)) + 1
        return {
            "transport": transport,
            "inbound": inbound,          # run this on the Xray server
            "outbound": outbound,        # import into the client
            "share_url": link,           # vless:// one-line import
            "public_key": public,
            "client_uuid": uuid,
            "server_names": profile["server_names"],
        }

    # ---- optional pinned runtime ---------------------------------------------
    def runtime_source(self) -> str:
        """'env' | 'image' | '' — where the pinned Xray comes from."""
        if self.cfg.xray_binary and self.cfg.xray_sha256:
            return "env"
        return "image" if baked_xray() else ""

    def _effective_binary(self) -> tuple[str, str] | None:
        """(path, sha256) from env pin or the image bake, env wins."""
        if self.cfg.xray_binary and self.cfg.xray_sha256:
            return self.cfg.xray_binary, self.cfg.xray_sha256
        return baked_xray()

    def runtime_configured(self) -> bool:
        return self._effective_binary() is not None

    def runtime_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def runtime_start(self) -> dict:
        if self.runtime_running():
            return {"ok": True, "already": True}
        pair = self._effective_binary()
        if pair is None:
            raise RuntimeError(
                "REALITY runtime needs a pinned Xray: set EMUNEL_XRAY_BINARY + "
                "EMUNEL_XRAY_SHA256, or build the image with the XRAY_VERSION "
                "build variable (see docs/RAILWAY.md)")
        binary = await asyncio.to_thread(verify_xray_binary, pair[0], pair[1])
        if not self._private_key:
            self.generate_keys()
        profile = dict(self.profile)
        profile["transport"] = "raw"
        inbound = build_inbound(profile, listen_host=self.cfg.reality_listen_host,
                                listen_port=int(self.profile["listen_port"]),
                                uuid=self.active_client_uuid(),
                                private_key=self._private_key)
        config = {
            "log": {"loglevel": "warning"},
            "inbounds": [inbound],
            "outbounds": [
                {"tag": "reality-target", "protocol": "freedom",
                 "settings": {"domainStrategy": "AsIs"}},
                {"tag": "direct", "protocol": "freedom", "settings": {}},
            ],
        }
        cfg_path = Path(self.state.data_dir()) / "reality_runtime.json"
        cfg_path.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        try:
            cfg_path.chmod(0o600)
        except OSError:
            pass
        self._proc_config = cfg_path
        self._proc = await asyncio.create_subprocess_exec(
            binary, "run", "-c", str(cfg_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._stderr_task = asyncio.create_task(self._drain_output())
        self.status.metrics["runtime_running"] = True
        self.status.metrics["runtime_started_at"] = time.time()
        stored = self.state.get(self.NAME, {}) or {}
        stored["runtime_restarts"] = int(stored.get("runtime_restarts", 0) or 0) + 1
        self.state.set(self.NAME, stored)
        self.status.metrics["runtime_restarts"] = stored["runtime_restarts"]
        await asyncio.sleep(0.5)
        if not self.runtime_running():
            raise RuntimeError("Xray exited immediately — check the engine log (bad key/target/port?)")
        self.log.info(f"REALITY runtime listening on {self.cfg.reality_listen_host}:"
                      f"{self.profile['listen_port']} (pinned Xray, source="
                      f"{self.runtime_source()})")
        return {"ok": True, "pid": self._proc.pid,
                "listen": f"{self.cfg.reality_listen_host}:{self.profile['listen_port']}"}

    async def _drain_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.log.info("[xray] " + text[:300])
        except (asyncio.CancelledError, Exception):
            pass

    async def runtime_stop(self) -> None:
        self.status.metrics["runtime_running"] = False
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError, Exception):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self.log.info(f"REALITY runtime stopped (pid was {self._proc.pid})")
            self._proc = None

    async def _soft_restart_runtime(self) -> None:
        try:
            await self.runtime_stop()
            await self.runtime_start()
        except Exception as exc:  # never crash the host on restart
            self.log.error(f"runtime soft-restart failed: {exc}")
            self.status.metrics["runtime_running"] = False

    async def restart(self) -> dict:
        """POST /api/engines/reality/restart — full re-init + runtime cycle."""
        await self.runtime_stop()
        stored = self.state.get(self.NAME, {}) or {}
        self.profile = self._merge_profile(stored.get("profile") or {})
        if self.runtime_configured():
            return await self.runtime_start()
        return {"ok": True, "runtime": "not configured",
                "generator": "reloaded"}
