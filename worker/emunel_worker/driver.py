"""Instance drivers for the EMUNEL Worker.

Two drivers share one interface:

* ``DockerDriver``   — production. Runs EMUNEL Core in an isolated container:
  CPU/memory/PID limits, read-only root filesystem, dropped capabilities,
  no-new-privileges, non-root user, private bridge network. The Docker socket
  is used only by the Worker itself; it is never mounted into Core containers.
* ``ProcessDriver``  — development/testing (no Docker available). Runs Core as
  a subprocess with OS resource limits (RLIMIT_AS / RLIMIT_CPU / RLIMIT_FSIZE)
  in its own session and a per-instance data directory. Isolation is weaker
  than container isolation and must not be used in production.

Selection: ``EMUNEL_WORKER_DRIVER=docker|process`` (default: docker if the
docker CLI is available, else process).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get

log = get("runtime", "emunel.worker.driver")


class DriverError(RuntimeError):
    pass


@dataclass
class LaunchSpec:
    instance_id: str
    deployment_id: str
    core_version: str
    api_token: str
    public_host: str = ""
    cpu_limit: float = 0.5          # docker --cpus
    memory_mb: int = 256            # docker --memory / RLIMIT_AS
    max_processes: int = 128        # docker --pids-limit
    port: int = 0                   # 0 = auto-allocate


@dataclass
class InstanceHandle:
    instance_id: str
    driver: str
    reference: str                  # container name / pid
    port: int
    started_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)


def port_is_free(port: int) -> bool:
    # No SO_REUSEADDR here: it makes the probe succeed on macOS even while
    # another process is actively listening, causing launch-time bind failures.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class PortAllocator:
    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end
        self.used: set[int] = set()

    def allocate(self, preferred: int | None = None) -> int:
        candidates = []
        if preferred and self.start <= preferred <= self.end:
            candidates.append(preferred)
        candidates.extend(range(self.start, self.end + 1))
        for port in candidates:
            if port in self.used:
                continue
            if port_is_free(port):
                self.used.add(port)
                return port
        raise DriverError("no free ports in allocation range")

    def release(self, port: int | None) -> None:
        if port:
            self.used.discard(port)


class BaseDriver:
    name = "base"

    def __init__(self, ports: PortAllocator, data_root: Path):
        self.ports = ports
        self.data_root = data_root
        self.handles: dict[str, InstanceHandle] = {}

    async def launch(self, spec: LaunchSpec) -> InstanceHandle:  # pragma: no cover
        raise NotImplementedError

    async def stop(self, instance_id: str, timeout: int = 10) -> None:  # pragma: no cover
        raise NotImplementedError

    async def remove(self, instance_id: str, timeout: int = 10) -> None:  # pragma: no cover
        raise NotImplementedError

    async def status(self, instance_id: str) -> dict:  # pragma: no cover
        raise NotImplementedError

    async def logs(self, instance_id: str, tail: int = 200) -> list[str]:
        return []

    def is_known(self, instance_id: str) -> bool:
        return instance_id in self.handles

    def known_instances(self) -> list[InstanceHandle]:
        return list(self.handles.values())


# ---------------------------------------------------------------------------
# Docker driver
# ---------------------------------------------------------------------------
class DockerDriver(BaseDriver):
    name = "docker"

    def __init__(self, ports: PortAllocator, data_root: Path, network: str = "emunel"):
        super().__init__(ports, data_root)
        self.network = network
        if shutil.which("docker") is None:
            raise DriverError("docker CLI not found")
        self._ensure_network()

    def _ensure_network(self) -> None:
        result = subprocess.run(
            ["docker", "network", "inspect", self.network],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            subprocess.run(
                ["docker", "network", "create", "--driver", "bridge", self.network],
                capture_output=True, text=True, timeout=30, check=True,
            )
            log.info("created docker network %s", self.network)

    async def _run(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise DriverError(f"docker {' '.join(args[:2])} timed out")
        if proc.returncode != 0:
            raise DriverError(stderr.decode(errors="replace").strip()[:500] or "docker command failed")
        return stdout.decode(errors="replace")

    async def launch(self, spec: LaunchSpec) -> InstanceHandle:
        port = self.ports.allocate(spec.port or None)
        name = f"emunel-inst-{spec.instance_id[:12]}"
        image = f"emunel/core:{spec.core_version}"
        data_dir = self.data_root / spec.instance_id
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._run(
                "run", "-d",
                "--name", name,
                "--label", "emunel.instance=" + spec.instance_id,
                "--label", "emunel.managed-by=emunel-worker",
                # resource limits
                "--cpus", str(spec.cpu_limit),
                "--memory", f"{spec.memory_mb}m",
                "--pids-limit", str(spec.max_processes),
                # isolation hardening
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--user", "65532:65532",
                "--network", self.network,
                # expose core port only on loopback of the worker host
                "-p", f"127.0.0.1:{port}:8000",
                "-e", f"EMUNEL_CORE_API_TOKEN={spec.api_token}",
                "-e", f"EMUNEL_PUBLIC_HOST={spec.public_host}",
                "-e", "EMUNEL_STATE_PATH=/data/state.json",
                "-v", f"{data_dir}:/data",
                image,
            )
        except DriverError:
            self.ports.release(port)
            raise
        handle = InstanceHandle(
            instance_id=spec.instance_id, driver="docker", reference=name, port=port,
            meta={"deployment_id": spec.deployment_id, "image": image, "api_token": spec.api_token},
        )
        self.handles[spec.instance_id] = handle
        log.info("docker launch %s -> %s (port %d)", spec.instance_id[:12], name, port)
        return handle

    async def stop(self, instance_id: str, timeout: int = 10) -> None:
        handle = self.handles.get(instance_id)
        if not handle:
            raise DriverError("instance not known to worker")
        await self._run("stop", "-t", str(timeout), handle.reference, timeout=timeout + 20)

    async def remove(self, instance_id: str, timeout: int = 10) -> None:
        handle = self.handles.pop(instance_id, None)
        if not handle:
            return
        try:
            await self._run("rm", "-f", handle.reference, timeout=30)
        finally:
            self.ports.release(handle.port)

    async def status(self, instance_id: str) -> dict:
        handle = self.handles.get(instance_id)
        if not handle:
            return {"running": False, "known": False}
        out = await self._run("inspect", "--format", "{{json .State}}", handle.reference)
        state = json.loads(out.strip())
        return {
            "running": bool(state.get("Running")),
            "known": True,
            "exit_code": state.get("ExitCode"),
            "started_at": state.get("StartedAt"),
            "port": handle.port,
            "reference": handle.reference,
        }

    async def logs(self, instance_id: str, tail: int = 200) -> list[str]:
        handle = self.handles.get(instance_id)
        if not handle:
            return []
        out = await self._run("logs", "--tail", str(tail), handle.reference, timeout=20)
        return out.splitlines()


# ---------------------------------------------------------------------------
# Process driver (development only)
# ---------------------------------------------------------------------------
class ProcessDriver(BaseDriver):
    name = "process"

    # Core-host engines -> their enable flags. Mirrors engines/config.py's
    # CORE_HOST_ENGINES / ENGINE_FLAG_VARS (the worker deliberately does NOT
    # import the engines package — it must stay import-lean).
    CORE_ENGINE_FLAGS = {
        "PreConnect": "EMUNEL_ENGINE_PRECONNECT_ENABLED",
        "Congestion": "EMUNEL_ENGINE_CONGESTION_ENABLED",
        "Compress": "EMUNEL_ENGINE_COMPRESS_ENABLED",
        "FEC": "EMUNEL_ENGINE_FEC_ENABLED",
    }
    # STABILIZATION: merged-module flags that run core-side children — when
    # any is on, the Core launches through the engines host and its own
    # manager activates the merged module (flag propagates via env verbatim)
    MERGED_CORE_FLAGS = {
        "Transport": "TRANSPORT_MERGED",
    }

    def __init__(self, ports: PortAllocator, data_root: Path, core_cmd: list[str] | None = None):
        super().__init__(ports, data_root)
        # Default: run Core with its own venv so its dependencies (cryptography
        # etc.) resolve independently of the worker's environment. The module
        # (raw Core vs engines.core_host) is decided PER LAUNCH in
        # _core_launch_module() — env pins, the operator's persisted hot
        # toggles and the engines kill-switch all flow through there.
        self.core_python = os.environ.get("EMUNEL_CORE_PYTHON", ".venv/bin/python")
        self.core_cmd = core_cmd      # full explicit override (tests)
        self.core_cwd = os.environ.get("EMUNEL_CORE_CWD", "")

    # ---- engines host decision ---------------------------------------------
    def _engines_root(self) -> str:
        explicit = os.environ.get("EMUNEL_ENGINES_ROOT", "")
        if explicit:
            return explicit
        if self.core_cwd:
            return str(Path(self.core_cwd).resolve().parent)
        return ""

    def _engine_toggles(self) -> dict:
        """The operator's persisted engine hot-toggles (state.json, written
        by the console's EngineManager on every toggle — shared volume in the
        unified deployment). Best-effort, stdlib only; {} when unreadable."""
        import json as _json

        candidates: list[Path] = []
        env_dir = os.environ.get("EMUNEL_ENGINE_DATA", "")
        if env_dir:
            candidates.append(Path(env_dir))
        worker_data = os.environ.get("EMUNEL_WORKER_DATA", "")
        if worker_data:
            candidates.append(Path(worker_data).parent / "engines")
        candidates.append(Path("/data/engines"))
        if self.core_cwd:
            candidates.append(Path(self.core_cwd).resolve().parent / ".emunel-data" / "engines")
        for base in candidates:
            try:
                raw = _json.loads((base / "state.json").read_text(encoding="utf-8"))
                enabled = ((raw.get("engines") or {}).get("EngineToggles") or {}).get("enabled")
                if isinstance(enabled, dict):
                    return {str(k): bool(v) for k, v in enabled.items()
                            if isinstance(v, bool)}
            except (OSError, ValueError, AttributeError):
                continue
        return {}

    def _core_launch_module(self) -> tuple[str, dict[str, str]]:
        """Decide the launch module for a Core: raw 'emunel_core' or the
        engines host 'engines.core_host'. Returns (module, extra_env).

        Precedence (same rules the console's EngineManager applies):
          1. EMUNEL_ENGINES_ENABLED=0 — engines off entirely, raw Core.
          2. EMUNEL_CORE_MODULE pinned in this worker's environment (the
             unified entrypoint's boot pre-check, or the operator) — respected
             as-is; an explicit 'emunel_core' pin cannot be overridden.
          3. any core-host engine active — via its env flag OR the
             operator's persisted hot-toggle — -> engines host, with the
             toggle decisions translated to EMUNEL_ENGINE_*_ENABLED so the
             Core's own manager activates the same engines.
          4. otherwise the raw Core — exactly the previous behaviour."""
        if os.environ.get("EMUNEL_ENGINES_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return os.environ.get("EMUNEL_CORE_MODULE", "") or "emunel_core", {}
        pinned = os.environ.get("EMUNEL_CORE_MODULE", "")
        if pinned:
            return pinned, {}
        toggles = self._engine_toggles()
        extra: dict[str, str] = {}
        want_host = False
        for name, flag in self.CORE_ENGINE_FLAGS.items():
            raw = os.environ.get(flag, "")
            env_off = raw.strip().lower() in ("0", "false", "no", "off")
            env_on = raw.strip().lower() in ("1", "true", "yes", "on")
            if env_off:
                extra[flag] = "0"          # env kill-switch wins, same as console
                continue
            if toggles.get(name, False) or env_on:
                want_host = True
                extra[flag] = "1"
            elif name in toggles:           # explicitly hot-disabled by operator
                extra[flag] = "0"
        # merged Transport module (STABILIZATION): TRANSPORT_MERGED=true in
        # this worker's env (set by the console pre-check) launches Cores
        # through the engines host with the module flag passed verbatim
        for name, flag in self.MERGED_CORE_FLAGS.items():
            if os.environ.get(flag, "").strip().lower() in ("1", "true", "yes", "on"):
                want_host = True
            elif toggles.get(name, False):
                want_host = True
                extra[flag] = "1"
        if not want_host:
            return "emunel_core", {}
        return "engines.core_host", extra

    def _limits(self, spec: LaunchSpec):
        import resource

        def apply() -> None:  # runs in the child before exec
            mem_bytes = spec.memory_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
                resource.setrlimit(resource.RLIMIT_CPU, (60 * 60, 60 * 60 + 60))
                resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
                # NOTE: RLIMIT_NPROC is deliberately NOT set here. The kernel
                # enforces it per *real UID*, not per process tree — in the
                # unified single-service deployment (Console + Worker + every
                # Core share one user) it counts unrelated processes/threads and
                # makes healthy Cores abort (SIGABRT inside uvicorn startup)
                # once the UID crosses the limit. Container-scoped PID limits
                # belong to the Docker driver (--pids-limit), which keeps the
                # protection where the semantics are correct.
            except (ValueError, OSError):
                pass
            os.umask(0o077)

        return apply

    async def launch(self, spec: LaunchSpec) -> InstanceHandle:
        port = self.ports.allocate(spec.port or None)
        data_dir = self.data_root / spec.instance_id
        data_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            "EMUNEL_CORE_API_TOKEN": spec.api_token,
            "EMUNEL_STATE_PATH": str(data_dir / "state.json"),
            "EMUNEL_PUBLIC_HOST": spec.public_host,
            "EMUNEL_LOG_JSON": "1",
            "PORT": str(port),
        })
        env.pop("PYTHONPATH", None)  # never leak worker deps into Core
        # Engines: per-launch decision (env pins + the operator's persisted
        # hot-toggles). Through the engines host the Core needs the repo root
        # on sys.path to import the engines package — and nothing else (no
        # worker deps leak). Each instance gets its OWN engine state dir so
        # a Core can never clobber the console's shared state.json (nor the
        # other way around).
        core_module, engine_flags = self._core_launch_module()
        if core_module != "emunel_core":
            engines_root = self._engines_root()
            if engines_root:
                env["PYTHONPATH"] = engines_root
            env["EMUNEL_ENGINE_DATA"] = str(data_dir / "engines")
            env.update(engine_flags)
        core_cmd = self.core_cmd or [self.core_python, "-m", core_module]

        out_path = data_dir / "core.log"
        out_fh = out_path.open("ab", buffering=0)
        proc = await asyncio.create_subprocess_exec(
            *core_cmd, "--port", str(port),
            stdout=out_fh, stderr=subprocess.STDOUT,
            env=env, preexec_fn=self._limits(spec), start_new_session=True,
            cwd=self.core_cwd or None,
        )
        handle = InstanceHandle(
            instance_id=spec.instance_id, driver="process", reference=str(proc.pid), port=port,
            meta={"deployment_id": spec.deployment_id, "pid": proc.pid, "log_file": str(out_path),
                  "api_token": spec.api_token},
        )
        self.handles[spec.instance_id] = handle
        log.info("process launch %s -> pid %d (port %d)", spec.instance_id[:12], proc.pid, port)
        return handle

    async def stop(self, instance_id: str, timeout: int = 10) -> None:
        handle = self.handles.get(instance_id)
        if not handle:
            raise DriverError("instance not known to worker")
        pid = int(handle.reference)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(timeout * 10):
            await asyncio.sleep(0.1)
            if not self._pid_alive(pid):
                return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def remove(self, instance_id: str, timeout: int = 10) -> None:
        handle = self.handles.pop(instance_id, None)
        if not handle:
            return
        try:
            await self.stop(instance_id, timeout)
        except DriverError:
            pass
        finally:
            self.ports.release(handle.port)
            data_dir = self.data_root / instance_id
            shutil.rmtree(data_dir, ignore_errors=True)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    async def status(self, instance_id: str) -> dict:
        handle = self.handles.get(instance_id)
        if not handle:
            return {"running": False, "known": False}
        alive = self._pid_alive(int(handle.reference))
        return {
            "running": alive, "known": True,
            "pid": int(handle.reference),
            "port": handle.port, "reference": handle.reference,
            "started_at": handle.started_at,
        }

    async def logs(self, instance_id: str, tail: int = 200) -> list[str]:
        handle = self.handles.get(instance_id)
        if not handle:
            return []
        path = Path(handle.meta.get("log_file", ""))
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-tail:]


def select_driver(data_root: Path) -> tuple[BaseDriver, str]:
    ports = PortAllocator(
        int(os.environ.get("EMUNEL_WORKER_PORT_START", "19000")),
        int(os.environ.get("EMUNEL_WORKER_PORT_END", "19999")),
    )
    chosen = os.environ.get("EMUNEL_WORKER_DRIVER", "").strip().lower()
    if not chosen:
        chosen = "docker" if shutil.which("docker") else "process"
    if chosen == "docker":
        try:
            return DockerDriver(ports, data_root), "docker"
        except DriverError as exc:
            log.warning("docker driver unavailable (%s); falling back to process driver", exc)
            chosen = "process"
    if chosen == "process":
        return ProcessDriver(ports, data_root), "process"
    raise DriverError(f"unknown driver: {chosen}")
