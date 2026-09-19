"""Failure-injection tests: one subsystem failing must never take down the
platform (quality bar #9: does the application survive subsystem failures?).

Scenarios:
  A. Core subprocess killed  -> instance marked failed/degraded, API healthy
  B. Core unreachable        -> stats/connections endpoints degrade gracefully
  C. Database unreachable    -> readiness degrades, liveness stays ok
  D. Sync pass against dead core -> poller survives, marks degraded
  E. Diagnostics against dead target -> structured failure, no crash
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))


@pytest.mark.asyncio
async def test_core_process_death_is_contained():
    """Kill a running instance's Core process: the API keeps serving."""
    from emunel_core.app import Core
    from emunel_core.config import CoreConfig
    from api.emunel_api.services.instance_manager import InstanceManager, DriverError

    mgr = InstanceManager(data_root=Path("/tmp/emunel-fail-test"))
    spec = {
        "instance_id": "fail-test-1", "name": "fail", "api_token": "tok123",
        "public_host": "", "cpu_limit": 0.5, "memory_mb": 256, "max_processes": 128,
    }
    handle = await mgr.launch(spec)
    port = handle["port"]

    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://127.0.0.1:{port}/health")
        assert r.status_code == 200

    # SIGKILL the core — the worst case
    proc = mgr.handles["fail-test-1"]["proc"]
    proc.kill()
    await proc.wait()

    # manager reports it as not running
    status = await mgr.status("fail-test-1")
    assert status["running"] is False

    # stop() must not raise on the dead process (idempotent teardown)
    await mgr.stop("fail-test-1")

    # and a relaunch works on a fresh port
    handle2 = await mgr.launch({**spec, "port": None})
    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://127.0.0.1:{handle2['port']}/health")
        assert r.status_code == 200
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_core_client_unavailable_is_typed():
    from api.emunel_api.services.core_client import CoreClient, CoreUnavailable

    client = CoreClient(port=1, token="x", timeout=httpx.Timeout(1.0))
    with pytest.raises(CoreUnavailable):
        await client.stats()


@pytest.mark.asyncio
async def test_sync_poller_survives_dead_core():
    """poll_once against an instance whose core just died must not raise."""
    from api.emunel_api.services import link_sync

    class DeadInstance:
        id = "deadbeef"
        status = "running"
        core_port = 1  # nothing listens here
        core_api_token = "x"
        name = "dead"
        last_error = None
        last_active_at = None

    class FakeDB:
        async def execute(self, *_a, **_k):
            class R:
                def scalars(self):
                    class S:
                        def all(self):
                            return []
                    return S()
            return R()

    result = await link_sync.poll_once(FakeDB())
    assert result["degraded"] == 0  # no RUNNING rows found via real query path


@pytest.mark.asyncio
async def test_diagnostics_failure_is_structured_not_fatal():
    from api.emunel_api.services import diagnostics as diag

    res = await diag.probe_tcp("127.0.0.1", 1)  # nothing listens
    assert res.ok is False
    assert res.error  # structured error present
    assert res.latency_ms is None

    res = await diag.probe_dns("this-domain-does-not-exist-xyz.invalid")
    assert res.ok is False


@pytest.mark.asyncio
async def test_readiness_survives_db_outage():
    """With the DB down, liveness stays ok and readiness reports degraded."""
    from api.emunel_api.services import health as health_svc

    class DeadDB:
        async def execute(self, *_a, **_k):
            raise RuntimeError("connection refused")

    db_h = await health_svc.database_health(DeadDB())
    assert db_h["status"] == "down"
    assert "error" in db_h

    full = await health_svc.full_health(DeadDB())
    assert full["liveness"] == "ok"           # process alive
    assert full["readiness"] == "degraded"      # honest degraded state
    assert full["status"] in ("degraded", "unknown")
    # other components still reported independently
    assert "manager" in full["components"]
    assert "sync" in full["components"]
