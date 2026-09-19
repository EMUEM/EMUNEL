"""Modern VMess AEAD via an explicitly installed, SHA256-pinned Xray runtime.

Core never implements VMess cryptography. A bounded pool of per-link Xray
processes binds only to loopback; each inbound accepts precisely that link's
UUID. This prevents using a different user's credentials on a quota-free path.
Counters measure encrypted WebSocket bytes, not VMess plaintext payload bytes.
No binary download, shell execution, public listener, or external API is used.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
from uuid import UUID

from fastapi import WebSocket
from websockets.asyncio.client import connect

from .base import RelayContext, ws_client_ip


class RuntimeUnavailable(RuntimeError):
    pass


def verify_binary(cfg) -> str:
    """A digest pin is required; operator must verify its trusted provenance."""
    path = Path(cfg.xray_binary)
    if not cfg.xray_binary or not path.is_absolute() or not path.is_file():
        raise RuntimeUnavailable("VMess requires EMUNEL_XRAY_BINARY (absolute installed Xray path)")
    expected = cfg.xray_sha256.lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RuntimeUnavailable("VMess requires EMUNEL_XRAY_SHA256 from a trusted release")
    if not os.access(path, os.X_OK):
        raise RuntimeUnavailable("Xray binary is not executable")
    if path.stat().st_mode & 0o022:
        raise RuntimeUnavailable("Xray binary must not be group/world writable")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if not secrets.compare_digest(digest.hexdigest(), expected):
        raise RuntimeUnavailable("Xray SHA256 mismatch; refusing execution")
    return str(path)


class XrayRuntime:
    """Lazy, reference-counted per-link runtimes; stopped after the last relay."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = asyncio.Lock()
        self._entries: dict[str, dict] = {}
        self._closed = False

    @staticmethod
    def config(uuid: str, port: int) -> dict:
        # Xray rejects legacy alterId clients; only AEAD clients are advertised.
        UUID(uuid)
        return {
            "log": {"loglevel": "warning"},
            "inbounds": [{
                "tag": "emunel-vmess", "listen": "127.0.0.1", "port": port,
                "protocol": "vmess",
                "settings": {"clients": [{"id": uuid, "alterId": 0}],
                             "disableInsecureEncryption": True},
                "streamSettings": {"network": "ws", "security": "none",
                                   "wsSettings": {"path": f"/vmess-ws/{uuid}"}},
            }],
            "outbounds": [{"tag": "direct", "protocol": "freedom", "settings": {}}],
        }

    async def _start(self, uuid: str) -> dict:
        binary = await asyncio.to_thread(verify_binary, self.cfg)
        directory = tempfile.TemporaryDirectory(prefix="emunel-xray-")
        proc = None
        try:
            # Xray chooses no dynamic port itself. Reserve a loopback port then
            # release immediately before launch; startup fails closed on collision.
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            config_path = Path(directory.name) / "config.json"
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as target:
                json.dump(self.config(uuid, port), target)
            proc = await asyncio.create_subprocess_exec(
                binary, "run", "-config", str(config_path),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(self.cfg.upstream_connect_timeout):
                while True:
                    if proc.returncode is not None:
                        raise RuntimeUnavailable("Xray exited during startup; verify installed version/config compatibility")
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                    except OSError:
                        await asyncio.sleep(0.05)
                        continue
                    writer.close()
                    await writer.wait_closed()
                    break
            if proc.returncode is not None:
                raise RuntimeUnavailable("Xray exited during startup")
            return {"proc": proc, "directory": directory, "port": port, "refs": 0}
        except BaseException:
            if proc is not None:
                await self._stop_process(proc)
            directory.cleanup()
            raise

    @staticmethod
    async def _stop_process(proc):
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def _stop_entry(self, entry):
        try:
            await self._stop_process(entry["proc"])
        finally:
            entry["directory"].cleanup()

    @asynccontextmanager
    async def acquire(self, uuid: str):
        UUID(uuid)
        async with self._lock:
            if self._closed:
                raise RuntimeUnavailable("VMess runtime shutting down")
            entry = self._entries.get(uuid)
            if entry is None:
                if len(self._entries) >= self.cfg.xray_max_runtimes:
                    raise RuntimeUnavailable("VMess concurrent runtime limit reached")
                try:
                    entry = await self._start(uuid)
                except (OSError, TimeoutError) as exc:
                    raise RuntimeUnavailable("Xray failed to start") from exc
                self._entries[uuid] = entry
            elif entry["proc"].returncode is not None:
                raise RuntimeUnavailable("Xray process exited")
            entry["refs"] += 1
        try:
            yield f"ws://127.0.0.1:{entry['port']}/vmess-ws/{uuid}"
        finally:
            async with self._lock:
                entry["refs"] -= 1
                if entry["refs"] == 0 and self._entries.get(uuid) is entry:
                    self._entries.pop(uuid)
                    await self._stop_entry(entry)

    async def close(self):
        async with self._lock:
            self._closed = True
            entries, self._entries = self._entries, {}
            for entry in entries.values():
                await self._stop_entry(entry)


async def vmess_ws_tunnel(ctx: RelayContext, runtime: XrayRuntime, ws: WebSocket, uuid: str):
    await ws.accept()
    link = ctx.links.get(uuid)
    if link is None or link.protocol != "vmess-ws" or not link.is_allowed():
        await ws.close(code=1008, reason="not authorized")
        return
    _vmess_ip = ws_client_ip(ws)
    if not ctx.connections.ip_allowed(link, _vmess_ip):
        ctx.stats.add_error("ip limit reached")
        await ws.close(code=1008, reason="ip limit reached")
        return
    conn_id = secrets.token_urlsafe(6)
    tasks = []

    async def account(data: bytes) -> bool:
        # No batching: low-volume sessions still obey deletion, expiry and quota.
        current = ctx.links.get(uuid)
        if current is None or current.protocol != "vmess-ws" or not current.is_allowed():
            return False
        ctx.stats.add_traffic(len(data))
        ctx.connections.add_bytes(conn_id, len(data))
        return await ctx.links.use(uuid, len(data))

    try:
        async with runtime.acquire(uuid) as url:
            async with connect(url, proxy=None, compression=None,
                               open_timeout=ctx.cfg.upstream_connect_timeout,
                               max_size=4 * 1024 * 1024) as upstream:
                ctx.connections.register(conn_id, uuid=uuid, ip=ws_client_ip(ws), transport="vmess-ws")
                ctx.stats.add_request()

                async def upload():
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        data = msg.get("bytes")
                        if data is None:
                            await ws.close(code=1003, reason="binary frames required")
                            return
                        if not await account(data):
                            await ws.close(code=1008, reason="quota exceeded or link disabled")
                            return
                        await upstream.send(data)

                async def download():
                    async for data in upstream:
                        if not isinstance(data, bytes) or not await account(data):
                            await ws.close(code=1008, reason="quota exceeded or link disabled")
                            return
                        await ws.send_bytes(data)

                async def policy_watch():
                    while True:
                        await asyncio.sleep(0.25)
                        current = ctx.links.get(uuid)
                        if current is None or not current.is_allowed() or current.protocol != "vmess-ws":
                            await ws.close(code=1008, reason="link disabled or expired")
                            return

                tasks = [asyncio.create_task(fn()) for fn in (upload, download, policy_watch)]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
    except RuntimeUnavailable as exc:
        ctx.stats.add_error(str(exc))
        with suppress(Exception):
            await ws.close(code=1013, reason=str(exc)[:120])
    except Exception as exc:
        ctx.stats.add_error(f"VMess relay: {type(exc).__name__}")
        with suppress(Exception):
            await ws.close(code=1011, reason="VMess runtime unavailable")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ctx.connections.remove(conn_id)
        ctx.schedule_save()
        with suppress(Exception):
            await ws.close()
