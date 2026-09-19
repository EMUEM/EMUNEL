"""EMUNEL InstanceManager — launches and supervises isolated EMUNEL Core runtimes.

Ported from Lunel's Worker ProcessDriver model, unified into the API process:

* Each Instance runs as its own subprocess (``python -m emunel_core``) with
  its own loopback port, its own state file and its own management token.
* Resource limits are applied via rlimits in the child before exec
  (RLIMIT_AS / RLIMIT_CPU / RLIMIT_FSIZE / RLIMIT_NPROC).
* A JSON registry records every launched instance so they can be relaunched
  with identical credentials after a process restart.
* The manager never exposes ``core_api_token`` outside this module boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("emunel.api.instance_manager")

PORT_RANGE_START = int(os.environ.get("EMUNEL_PORT_RANGE_START", "18100"))
PORT_RANGE_END = int(os.environ.get("EMUNEL_PORT_RANGE_END", "18999"))
DATA_ROOT = Path(os.environ.get("EMUNEL_DATA_ROOT", "./emunel-data"))
REGISTRY_PATH = DATA_ROOT / "instances.json"
CORE_STARTUP_TIMEOUT = float(os.environ.get("EMUNEL_CORE_STARTUP_TIMEOUT", "15"))


class DriverError(RuntimeError):
    pass


class PortAllocator:
    """Loopback port allocation with liveness probing (Lunel Worker pattern,
    hardened): a candidate port is bind-tested before it is handed out, so
    orphaned processes from a previous unclean shutdown cannot cause a
    silent double-assignment."""

    def __init__(self, start: int, end: int):
        self._free = set(range(start, end))
        self._taken: dict[int, str] = {}

    @staticmethod
    def _bindable(port: int) -> bool:
        import socket as _socket

        try:
            with _socket.socket() as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def allocate(self, owner: str, preferred: Optional[int] = None) -> int:
        if preferred is not None and preferred not in self._taken and self._bindable(preferred):
            self._free.discard(preferred)
            self._taken[preferred] = owner
            return preferred
        while self._free:
            port = min(self._free)
            self._free.discard(port)
            if self._bindable(port):
                self._taken[port] = owner
                return port
        raise DriverError("no free ports in instance port range")

    def release(self, port: int) -> None:
        self._taken.pop(port, None)
        self._free.add(port)

    def mark_taken(self, port: int, owner: str) -> None:
        """Re-adopt a port discovered from the registry after restart."""
        self._free.discard(port)
        self._taken[port] = owner


class Registry:
    """Durable record of launched instances (survives process restarts)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, data: dict) -> None:
        async def _save():
            async with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self.path)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_save())
        except RuntimeError:
            pass

    def snapshot(self) -> dict:
        return self.load()


class InstanceManager:
    def __init__(self, data_root: Path = DATA_ROOT):
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.ports = PortAllocator(PORT_RANGE_START, PORT_RANGE_END)
        self.registry = Registry(REGISTRY_PATH)
        # instance_id -> {"proc": Process, "port": int, "spec": dict}
        self.handles: dict[str, dict] = {}
        self._recovered = False

    # ------------------------------------------------------------------
    # subprocess plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _rlimits(memory_mb: int, max_processes: int):
        def apply() -> None:  # runs in the child before exec
            try:
                mem = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
                resource.setrlimit(resource.RLIMIT_CPU, (3600, 3660))
                resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
                try:
                    resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
                except (ValueError, OSError):
                    pass
                os.umask(0o077)
            except (ValueError, OSError):
                pass

        return apply

    def _core_cmd(self) -> list[str]:
        # In containers / venvs we run with the same interpreter; PYTHONPATH
        # points at the repo root so ``emunel_core`` resolves.
        return [sys.executable, "-m", "emunel_core"]

    def _core_env(self, spec: dict) -> dict:
        env = os.environ.copy()
        # The Core package lives at <repo>/core/emunel_core — make it importable
        # in the subprocess (overridable for container layouts).
        core_home = os.environ.get("EMUNEL_CORE_HOME") or str(Path(__file__).resolve().parents[3] / "core")
        env.update(
            {
                "EMUNEL_CORE_API_TOKEN": spec["api_token"],
                "EMUNEL_CORE_STATE_PATH": str(spec["state_path"]),
                "EMUNEL_CORE_PUBLIC_HOST": spec.get("public_host") or "",
                "EMUNEL_CORE_LOG_JSON": "0",
                "PYTHONPATH": core_home,
            }
        )
        # never leak API-level secrets into the Core process
        for key in ("EMUNEL_SECRET_KEY", "EMUNEL_JWT_SECRET_KEY", "EMUNEL_ADMIN_PASSWORD", "DATABASE_URL"):
            env.pop(key, None)
        return env

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def launch(self, spec: dict) -> dict:
        """Launch an instance Core subprocess. Idempotent: a known running
        instance is stopped first, keeping its state dir (links survive)."""
        iid = spec["instance_id"]
        if iid in self.handles:
            await self.stop(iid)

        port = self.ports.allocate(iid, preferred=spec.get("port"))
        state_dir = self.data_root / iid
        state_dir.mkdir(parents=True, exist_ok=True)
        full_spec = {
            **spec,
            "port": port,
            "state_path": str(state_dir / "state.json"),
        }

        log_fh = (state_dir / "core.log").open("ab", buffering=0)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._core_cmd(),
                "--port", str(port),
                "--host", "127.0.0.1",
                "--state-path", full_spec["state_path"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=self._core_env(full_spec),
                preexec_fn=self._rlimits(full_spec.get("memory_mb", 256), full_spec.get("max_processes", 128)),
            )
        except OSError as exc:
            self.ports.release(port)
            raise DriverError(f"core launch failed: {exc}") from exc
        finally:
            # keep fd open in parent only until exec finished; closing it here
            # is safe because the child duplicated what it needs
            log_fh.close()

        self.handles[iid] = {"proc": proc, "port": port, "spec": full_spec}

        # wait for /health AND verify the listener accepts OUR management
        # token — a stale orphan process on the same port must not pass for
        # a successful launch.
        healthy = await self._wait_healthy_and_ours(port, spec["api_token"], CORE_STARTUP_TIMEOUT)
        if not healthy:
            await self.stop(iid)
            raise DriverError("core did not become healthy in time (check core.log)")

        self._registry_put(full_spec)
        logger.info("launched instance %s on port %d (pid %s)", iid[:12], port, proc.pid)
        return {"port": port, "pid": proc.pid}

    async def stop(self, instance_id: str, timeout: float = 10.0) -> None:
        handle = self.handles.pop(instance_id, None)
        if handle is None:
            return
        proc: asyncio.subprocess.Process = handle["proc"]
        self.ports.release(handle["port"])
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        logger.info("stopped instance %s", instance_id[:12])

    async def remove(self, instance_id: str) -> None:
        """Stop and delete the instance state dir (links and counters die)."""
        await self.stop(instance_id)
        state_dir = self.data_root / instance_id
        if state_dir.exists():
            import shutil

            shutil.rmtree(state_dir, ignore_errors=True)
        self._registry_remove(instance_id)

    async def restart(self, spec: dict) -> dict:
        return await self.launch(spec)

    # ------------------------------------------------------------------
    # status / health
    # ------------------------------------------------------------------
    async def _wait_healthy_and_ours(self, port: int, token: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/health")
                    if r.status_code == 200:
                        # ownership proof: our bearer token must be accepted
                        m = await client.get(
                            f"http://127.0.0.1:{port}/core/api/stats",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        if m.status_code == 200:
                            return True
                        if m.status_code == 401:
                            raise DriverError(
                                f"port {port} is served by a foreign core process"
                            )
                except DriverError:
                    raise
                except (httpx.HTTPError, OSError):
                    pass
                await asyncio.sleep(0.2)
        return False

    async def status(self, instance_id: str) -> dict:
        handle = self.handles.get(instance_id)
        if handle is None:
            return {"running": False, "known": False}
        proc = handle["proc"]
        alive = proc.returncode is None
        out = {"running": alive, "known": True, "port": handle["port"]}
        if alive:
            probe = await self._probe_core_health(handle["port"])
            out["healthy"] = probe is not None
            out["core_health"] = probe
        else:
            out["exit_code"] = proc.returncode
        return out

    async def _probe_core_health(self, port: int, timeout: float = 2.5) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(f"http://127.0.0.1:{port}/health")
                if r.status_code == 200:
                    return r.json()
        except (httpx.HTTPError, OSError):
            pass
        return None

    # ------------------------------------------------------------------
    # registry (restart recovery)
    # ------------------------------------------------------------------
    def _registry_put(self, spec: dict) -> None:
        data = self.registry.load()
        data[spec["instance_id"]] = {
            k: spec.get(k)
            for k in (
                "instance_id", "port", "state_path", "api_token", "public_host",
                "memory_mb", "max_processes", "cpu_limit", "name", "protocols",
            )
        }
        self.registry.save(data)

    def _registry_remove(self, instance_id: str) -> None:
        data = self.registry.load()
        data.pop(instance_id, None)
        self.registry.save(data)

    def registry_specs(self) -> list[dict]:
        return list(self.registry.load().values())

    async def recover(self) -> int:
        """After a restart, relaunch registered instances with identical
        credentials (state files are preserved, so links survive)."""
        if self._recovered:
            return 0
        self._recovered = True
        count = 0
        for spec in self.registry_specs():
            iid = spec.get("instance_id")
            if not iid or iid in self.handles:
                continue
            # adopt the recorded port so we never double-allocate
            try:
                self.ports.mark_taken(int(spec.get("port") or 0), iid)
            except (TypeError, ValueError):
                pass
            try:
                await self.launch({**spec, "port": int(spec.get("port") or 0) or None})
                count += 1
            except DriverError as exc:
                logger.warning("recovery failed for %s: %s", str(iid)[:12], exc)
        if count:
            logger.info("recovered %d instance(s) after restart", count)
        return count

    async def shutdown(self) -> None:
        for iid in list(self.handles):
            await self.stop(iid)


# module-level singleton wired by main.py lifespan
manager: Optional[InstanceManager] = None


def get_manager() -> InstanceManager:
    if manager is None:
        raise RuntimeError("InstanceManager not initialised (API startup incomplete)")
    return manager
