"""Regression tests for the instance-deploy / engine-plumbing fixes.

Root cause chain (operator report: 'the final stage of instance creation
fails with a 500 in the presence of the engines'):

1. PostgreSQL boolean literals — asyncpg rejects `1` for BOOLEAN columns
   while SQLite happily accepts it. Three spots did this:
     * services/deployments.py  INSERT instance_links ... VALUES (..., 1, ...)
       -> EVERY new-instance deploy failed at the link-provisioning final
          stage on PostgreSQL ("selected protocol could not be provisioned")
     * services/links.py        WHERE ... active = 1
       -> subscription quota aggregate raised on PG (silent unlimited)
     * db.py _seed_default_admin is_admin 1
       -> brand-new PG databases crashed the console at boot
   All use the TRUE keyword now (valid in BOTH dialects, SQLite >= 3.23).

2. Core-side engines could never run: the unified entrypoint spawned the
   worker BEFORE the engines layer decided EMUNEL_CORE_MODULE, and the
   worker's env copy never saw it; hot-toggles were never propagated at
   all. main.py now pre-decides (env path) and the worker re-decides per
   launch (env + persisted toggles), translating toggles to per-engine env
   flags so the Core's own manager activates the same engines.

3. Shared engine state: a Core launched through engines.core_host
   inherited the console's EMUNEL_ENGINE_DATA — its state flushes would
   destroy the console's toggles/metrics (and vice versa). The worker and
   the core host now give every Core its own state dir.

These tests pin all three fix families without needing a live PostgreSQL
(the SQL scan proves the dialect-neutrality; the SQLite functional tests
prove the new literals behave; the worker tests prove the decision matrix).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "console" / "api"))
sys.path.insert(0, str(ROOT))


# ═════════════════════════════════════════════════════════════════════════
# A. SQL dialect neutrality — no integer literals into BOOLEAN columns
# ═════════════════════════════════════════════════════════════════════════
def _console_sources() -> list[Path]:
    base = ROOT / "console" / "api" / "emunel_console"
    return [p for p in base.rglob("*.py") if p.is_file()]


@pytest.mark.parametrize("pattern,why", [
    (r"active\s*=\s*1\b", "instance_links.active is BOOLEAN on PostgreSQL — use TRUE"),
    (r"is_admin\s*=\s*1\b", "users.is_admin is BOOLEAN on PostgreSQL — use TRUE"),
    (r"is_disabled\s*=\s*1\b", "users.is_disabled is BOOLEAN on PostgreSQL — use TRUE"),
    (r"is_custom\s*=\s*1\b", "domains.is_custom is BOOLEAN on PostgreSQL — use TRUE"),
    (r"is_active\s*=\s*1\b", "domains.is_active is BOOLEAN on PostgreSQL — use TRUE"),
    (r"enabled\s*=\s*1\b", "workers.enabled is BOOLEAN on PostgreSQL — use TRUE"),
])
def test_no_boolean_integer_comparisons(pattern, why):
    offenders = []
    for path in _console_sources():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # only SQL strings (double-quoted python strings in this codebase)
            if re.search(pattern, line):
                offenders.append(f"{path.name}:{i}: {line.strip()[:90]}")
    assert not offenders, f"{why}\noffenders:\n" + "\n".join(offenders)


def test_no_boolean_integer_insert_literals():
    """The deploy-fatal INSERT pattern: a bare 1/0 in a VALUES list feeding
    a BOOLEAN column. The known-fixed statement in deployments.py must stay
    TRUE; scan for regressions of the same shape."""
    text = (ROOT / "console" / "api" / "emunel_console" / "services" / "deployments.py").read_text()
    # the provision INSERT must use the TRUE keyword for `active`
    assert re.search(
        r"INSERT INTO instance_links[^;]*?VALUES[^;]*?TRUE, 0, NULL", text, re.S), \
        "the deploy link-provision INSERT must write `TRUE` into `active`"
    # and no bare `, 1, 0, NULL` shape remains anywhere in the console
    for path in _console_sources():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"VALUES\s*\([^)]*\$[0-9]+,\s*1,\s*0,", line):
                pytest.fail(f"integer literal into a boolean column: "
                            f"{path.name}:{i}: {line.strip()[:90]}")


def test_links_aggregate_uses_true_keyword():
    text = (ROOT / "console" / "api" / "emunel_console" / "services" / "links.py").read_text()
    assert "active = TRUE" in text, "aggregate_quota must filter with `active = TRUE`"
    assert "active = 1" not in text


def test_seed_admin_uses_true_keyword():
    text = (ROOT / "console" / "api" / "emunel_console" / "db.py").read_text()
    assert "'Administrator', TRUE" in text, \
        "_seed_default_admin must insert TRUE into is_admin (PG BOOLEAN)"


# ═════════════════════════════════════════════════════════════════════════
# B. Functional (SQLite): the fixed statements really run
# ═════════════════════════════════════════════════════════════════════════
async def _fresh_sqlite_db(tmp_path):
    from emunel_console import config as console_cfg
    from emunel_console import db as db_mod

    await db_mod.close_db()
    original = console_cfg.settings.database_url
    dsn = f"sqlite:///{tmp_path / 't.db'}"
    os.environ["EMUNEL_DATABASE_URL"] = dsn
    console_cfg.settings.database_url = dsn   # the singleton caches env at import
    await db_mod.init_pool()
    return db_mod.db, original, console_cfg


async def _done_sqlite_db(original: str, console_cfg) -> None:
    from emunel_console import db as db_mod

    await db_mod.close_db()
    console_cfg.settings.database_url = original
    os.environ.pop("EMUNEL_DATABASE_URL", None)


@pytest.mark.asyncio
async def test_provision_insert_runs_on_sqlite(tmp_path):
    """The exact statement _provision_default_link runs — TRUE keyword and all
    (SQLite >= 3.23 parses TRUE as 1)."""
    db, original, cfg = await _fresh_sqlite_db(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        await db.execute(
            "INSERT INTO instances (id, user_id, name, slug, region, status, "
            "core_api_token, created_at, updated_at) "
            "VALUES ($1, 'u1', 'n', 'n', 'local', 'stopped', 'tok', $2, $2)",
            "inst1", now)
        await db.execute(
            "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
            "limit_bytes, expires_at, speed_limit_bytes, ip_limit, active, used_cache, "
            "used_at, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, 0, NULL, $10)",
            "l1", "inst1", "uuid-1", "label", "vless-ws",
            1024, None, None, None, now)
        active = await db.fetchval("SELECT active FROM instance_links WHERE id = 'l1'")
        assert bool(active) is True
    finally:
        await _done_sqlite_db(original, cfg)


@pytest.mark.asyncio
async def test_aggregate_quota_filter_runs_on_sqlite(tmp_path):
    db, original, cfg = await _fresh_sqlite_db(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        await db.execute(
            "INSERT INTO instances (id, user_id, name, slug, region, status, "
            "core_api_token, created_at, updated_at) "
            "VALUES ($1, 'u1', 'n', 'n', 'local', 'stopped', 'tok', $2, $2)",
            "inst1", now)
        for i, act in ((1, True), (2, False)):
            await db.execute(
                "INSERT INTO instance_links (id, instance_id, link_uuid, label, protocol, "
                "limit_bytes, expires_at, speed_limit_bytes, ip_limit, active, used_cache, "
                "used_at, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, NULL, $11)",
                f"l{i}", "inst1", f"uuid-{i}", "label", "vless-ws",
                1024 * i, None, None, None, act, now)
        rows = await db.fetch(
            "SELECT limit_bytes FROM instance_links "
            "WHERE instance_id = $1 AND active = TRUE", "inst1")
        assert len(rows) == 1 and int(rows[0]["limit_bytes"]) == 1024
    finally:
        await _done_sqlite_db(original, cfg)


# ═════════════════════════════════════════════════════════════════════════
# C. Worker launch decision (env + persisted toggles -> engines host)
# ═════════════════════════════════════════════════════════════════════════
def _driver(monkeypatch, tmp_path, core_cwd=None):
    from emunel_worker.driver import ProcessDriver

    monkeypatch.delenv("EMUNEL_CORE_MODULE", raising=False)
    monkeypatch.delenv("EMUNEL_ENGINES_ENABLED", raising=False)
    for flag in ProcessDriver.CORE_ENGINE_FLAGS.values():
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.delenv("EMUNEL_ENGINE_DATA", raising=False)
    monkeypatch.delenv("EMUNEL_WORKER_DATA", raising=False)
    drv = ProcessDriver.__new__(ProcessDriver)
    drv.core_python = sys.executable
    drv.core_cmd = None
    drv.core_cwd = str(core_cwd or (ROOT / "core"))
    return drv


def _write_toggles(tmp_path, enabled: dict):
    store = tmp_path / "engines"
    store.mkdir(parents=True, exist_ok=True)
    (store / "state.json").write_text(json.dumps({
        "version": 1, "saved_at": 1.0,
        "engines": {"EngineToggles": {"enabled": enabled}},
    }), encoding="utf-8")
    return store


def test_worker_default_raw_core(monkeypatch, tmp_path):
    drv = _driver(monkeypatch, tmp_path)
    module, extra = drv._core_launch_module()
    assert module == "emunel_core" and extra == {}


def test_worker_env_flag_switches_to_engines_host(monkeypatch, tmp_path):
    drv = _driver(monkeypatch, tmp_path)
    monkeypatch.setenv("EMUNEL_ENGINE_FEC_ENABLED", "1")
    module, extra = drv._core_launch_module()
    assert module == "engines.core_host"
    assert extra.get("EMUNEL_ENGINE_FEC_ENABLED") == "1"


def test_worker_hot_toggle_switches_to_engines_host(monkeypatch, tmp_path):
    """The reported breakage: hot-enabling FEC in the panel did nothing to
    instances. The worker must read the persisted toggle and translate it."""
    drv = _driver(monkeypatch, tmp_path)
    store = _write_toggles(tmp_path, {"FEC": True, "Compress": False})
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(store))
    module, extra = drv._core_launch_module()
    assert module == "engines.core_host"
    assert extra.get("EMUNEL_ENGINE_FEC_ENABLED") == "1"
    assert extra.get("EMUNEL_ENGINE_COMPRESS_ENABLED") == "0"


def test_worker_env_kill_switch_wins_over_toggle(monkeypatch, tmp_path):
    drv = _driver(monkeypatch, tmp_path)
    store = _write_toggles(tmp_path, {"FEC": True})
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(store))
    monkeypatch.setenv("EMUNEL_ENGINE_FEC_ENABLED", "0")
    module, extra = drv._core_launch_module()
    # the explicit env =0 kills the engine; with only FEC around and it
    # killed, nothing asks for the engines host -> the raw Core runs
    assert module == "emunel_core"
    assert extra.get("EMUNEL_ENGINE_FEC_ENABLED", "0") == "0"


def test_worker_engines_disabled_never_uses_host(monkeypatch, tmp_path):
    drv = _driver(monkeypatch, tmp_path)
    store = _write_toggles(tmp_path, {"FEC": True})
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(store))
    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "0")
    module, _ = drv._core_launch_module()
    assert module == "emunel_core"


def test_worker_pinned_module_respected(monkeypatch, tmp_path):
    drv = _driver(monkeypatch, tmp_path)
    monkeypatch.setenv("EMUNEL_CORE_MODULE", "engines.core_host")
    module, extra = drv._core_launch_module()
    assert module == "engines.core_host"
    monkeypatch.setenv("EMUNEL_CORE_MODULE", "emunel_core")
    store = _write_toggles(tmp_path, {"FEC": True})
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(store))
    module, _ = drv._core_launch_module()
    assert module == "emunel_core", "an explicit emunel_core pin must win"


def test_worker_toggle_store_lookup_falls_back(monkeypatch, tmp_path):
    """EMUNEL_ENGINE_DATA unset -> <worker-data>/../engines is consulted."""
    drv = _driver(monkeypatch, tmp_path)
    monkeypatch.setenv("EMUNEL_WORKER_DATA", str(tmp_path / "instances"))
    _write_toggles(tmp_path, {"PreConnect": True})  # tmp_path/engines
    assert drv._engine_toggles() == {"PreConnect": True}


def test_worker_corrupt_toggle_store_is_ignored(monkeypatch, tmp_path):
    drv = _driver(monkeypatch, tmp_path)
    store = tmp_path / "engines"
    store.mkdir(parents=True, exist_ok=True)
    (store / "state.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(store))
    assert drv._engine_toggles() == {}
    module, _ = drv._core_launch_module()
    assert module == "emunel_core"


# ═════════════════════════════════════════════════════════════════════════
# D. Engine state isolation (core host never uses the console's store)
# ═════════════════════════════════════════════════════════════════════════
def test_core_host_state_dir_is_per_instance(tmp_path):
    from engines.core_host import _per_instance_data_dir

    state = tmp_path / "instances" / "abc" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    got = _per_instance_data_dir(str(state))
    assert got == str(tmp_path / "instances" / "abc" / "engines")
    # the console's shared dir is NEVER the answer, whatever the env says
    shared = tmp_path / "shared-engines"
    os.environ["EMUNEL_ENGINE_DATA"] = str(shared)
    try:
        assert _per_instance_data_dir(str(state)) == got
    finally:
        os.environ.pop("EMUNEL_ENGINE_DATA", None)


def test_worker_launch_gives_core_its_own_engine_data(monkeypatch, tmp_path):
    """The env the worker hands to the Core must point EMUNEL_ENGINE_DATA at
    the instance dir, never the console's shared store (source-level check
    on the launch path + behavioural check of the decision helper)."""
    text = (ROOT / "worker" / "emunel_worker" / "driver.py").read_text()
    assert 'env["EMUNEL_ENGINE_DATA"] = str(data_dir / "engines")' in text, \
        "launch() must give each Core its own engine state dir"
    assert "PYTHONPATH" in text  # engines importable inside the host


def test_config_core_host_module_matrix(monkeypatch, tmp_path):
    from engines import config as cfg_mod

    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path / "e"))
    monkeypatch.delenv("EMUNEL_ENGINES_ENABLED", raising=False)
    for flag in cfg_mod.CORE_HOST_ENGINES:
        monkeypatch.delenv(cfg_mod.ENGINE_FLAG_VARS[flag], raising=False)
    assert cfg_mod.core_host_module() == ""
    monkeypatch.setenv("EMUNEL_ENGINE_CONGESTION_ENABLED", "true")
    assert cfg_mod.core_host_module() == "engines.core_host"
    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "0")
    assert cfg_mod.core_host_module() == ""
    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "1")
    monkeypatch.setenv("EMUNEL_ENGINE_CONGESTION_ENABLED", "0")
    assert cfg_mod.core_host_module() == ""


@pytest.mark.asyncio
async def test_hot_enable_core_engine_persists_toggle(tmp_path, monkeypatch):
    """The reported breakage, precisely: hot-enabling FEC from Engine Settings
    answered ok but NEVER persisted the toggle (set_engine_enabled returned
    early on 'not applicable in console host'), so the worker never learned
    to launch Cores through the engines host. The toggle must persist."""
    import asyncio

    from engines.config import parse_env
    from engines.manager import EngineManager

    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "1")
    monkeypatch.setenv("EMUNEL_ENGINE_DATA", str(tmp_path))
    monkeypatch.delenv("EMUNEL_ENGINE_FEC_ENABLED", raising=False)
    monkeypatch.delenv("EMUNEL_ENGINE_COMPRESS_ENABLED", raising=False)
    cfg = parse_env("console")
    mgr = EngineManager("console", cfg=cfg)
    await mgr.start()
    try:
        ok, message = await mgr.set_engine_enabled("FEC", True)
        assert ok is True, message
        assert "instances" in message, message
        persisted = mgr._persisted_toggles()
        assert persisted.get("FEC") is True, \
            "the FEC hot-enable must persist for the worker to act on it"
        ok2, message2 = await mgr.set_engine_enabled("FEC", False)
        assert ok2 is True
        assert mgr._persisted_toggles().get("FEC") is False
        # re-enable → persisted again
        await mgr.set_engine_enabled("FEC", True)
        assert mgr._persisted_toggles().get("FEC") is True
    finally:
        await mgr.stop()


def test_main_py_prechecks_before_worker_spawn():
    """The unified entrypoint must decide EMUNEL_CORE_MODULE BEFORE the
    worker subprocess copies the environment (source-order assertion)."""
    text = (ROOT / "main.py").read_text()
    decision = text.find("core_host_module()")
    worker_spawn = text.find('"-m", "emunel_worker"')
    assert decision != -1 and worker_spawn != -1
    assert decision < worker_spawn, \
        "the engines core-module decision must run before the worker is spawned"


def test_main_py_decision_env_isolation(monkeypatch, tmp_path):
    """core_host_module() must not touch the env when engines are off — the
    previous byte-identical behaviour (no EMUNEL_CORE_MODULE set)."""
    monkeypatch.setenv("EMUNEL_ENGINES_ENABLED", "0")
    os.environ.pop("EMUNEL_CORE_MODULE", None)
    from engines.config import core_host_module

    assert core_host_module() == ""
    assert "EMUNEL_CORE_MODULE" not in os.environ
