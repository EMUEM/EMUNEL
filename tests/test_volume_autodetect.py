"""Storage self-check (volume auto-verification at every boot) — suite.

Covers the boot-time storage announcement added with the volume auto-attach
operator task:

  * volume attached at /data (Railway-injected env or mountpoint) -> OK line
  * volume mounted at the WRONG path -> loud re-mount warning
  * no volume at all -> loud ephemeral-filesystem warning naming the fix
  * the check never raises (logging must never crash boot)
  * setup() runs the check before the worker spawn (source order)

The suite never imports root ``main.py`` (importing it boots the whole
platform — worker subprocess included); the REAL function is extracted from
the production source via AST instead, so a rename or behavior change in
main.py fails these tests rather than a stale copy passing them.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ═════════════════════════════════════════════════════════════════════════
# load the REAL _announce_storage from main.py without booting the platform
# ═════════════════════════════════════════════════════════════════════════
def _load_announce():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_announce_storage"
    )
    module = ast.Module(body=[fn], type_ignores=[])
    ns = {"os": os, "sys": sys, "__name__": "main._announce_storage"}
    exec(compile(module, str(ROOT / "main.py"), "exec"), ns)
    return ns["_announce_storage"]


@pytest.fixture()
def announce():
    return _load_announce()


def _run(announce, monkeypatch, capsys, *, env: dict[str, str], ismount) -> str:
    """Run the check under a controlled environment, return stderr output."""
    for var in ("RAILWAY_VOLUME_MOUNT_PATH", "RAILWAY_VOLUME_NAME"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        os.path, "ismount",
        lambda p: bool(ismount) if p == "/data" else False,
    )
    announce()
    captured = capsys.readouterr()
    return captured.err


# ═════════════════════════════════════════════════════════════════════════
# the good path
# ═════════════════════════════════════════════════════════════════════════
def test_attached_via_railway_env_ok(announce, monkeypatch, capsys):
    """Railway injects RAILWAY_VOLUME_MOUNT_PATH when a volume is attached."""
    out = _run(announce, monkeypatch, capsys,
               env={"RAILWAY_VOLUME_MOUNT_PATH": "/data"}, ismount=False)
    assert "[emunel] storage : volume attached at /data" in out
    assert "persists across redeploys" in out
    assert "WARNING" not in out


def test_attached_with_volume_name(announce, monkeypatch, capsys):
    out = _run(announce, monkeypatch, capsys,
               env={"RAILWAY_VOLUME_MOUNT_PATH": "/data",
                    "RAILWAY_VOLUME_NAME": "emunel-data"}, ismount=False)
    assert "(emunel-data)" in out
    assert "WARNING" not in out


def test_attached_via_mountpoint_without_env(announce, monkeypatch, capsys):
    """/data really is a mountpoint (docker/self-host) with no Railway env."""
    out = _run(announce, monkeypatch, capsys, env={}, ismount=True)
    assert "volume attached at /data" in out
    assert "WARNING" not in out


# ═════════════════════════════════════════════════════════════════════════
# the loud warning paths
# ═════════════════════════════════════════════════════════════════════════
def test_mis_mounted_path_warns(announce, monkeypatch, capsys):
    """Volume attached at the wrong path (e.g. /app/data) is data loss."""
    out = _run(announce, monkeypatch, capsys,
               env={"RAILWAY_VOLUME_MOUNT_PATH": "/app/data"}, ismount=False)
    assert "WARNING" in out
    assert "/app/data" in out
    assert "re-mount" in out


def test_missing_volume_warns_with_fix_command(announce, monkeypatch, capsys):
    """No volume: the warning must name the exact fix, copy-pasteable."""
    out = _run(announce, monkeypatch, capsys, env={}, ismount=False)
    assert "WARNING" in out
    assert "EPHEMERAL" in out
    assert "railway volume add -m /data" in out
    assert "RAILWAY_RUN_UID=0" in out


def test_env_var_wins_over_mountpoint_state(announce, monkeypatch, capsys):
    """A mis-mounted env path warns even if /data also looks mounted."""
    out = _run(announce, monkeypatch, capsys,
               env={"RAILWAY_VOLUME_MOUNT_PATH": "/app/data"}, ismount=True)
    assert "WARNING" in out
    assert "/app/data" in out


# ═════════════════════════════════════════════════════════════════════════
# never raises + wiring
# ═════════════════════════════════════════════════════════════════════════
def test_never_raises_on_unexpected_error(announce, monkeypatch, capsys):
    """Even a broken ismount must not crash boot — the check degrades to a
    one-line 'skipped' note."""
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_NAME", raising=False)

    def _boom(_p):
        raise RuntimeError("ismount exploded")

    monkeypatch.setattr(os.path, "ismount", _boom)
    announce()  # must not raise
    out = capsys.readouterr().err
    assert "status check skipped" in out


def test_setup_runs_storage_check_before_worker_spawn():
    """Source-order assertion (importing main.py would boot the platform):
    setup() must call _announce_storage() before the embedded worker is
    spawned, so the storage line is present in every deploy log."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    setup_idx = src.index("def setup()")
    announce_idx = src.index("_announce_storage()", setup_idx)
    worker_idx = src.index("# ---- embedded worker", setup_idx)
    assert setup_idx < announce_idx < worker_idx


def test_log_signature_prefix_is_stable(announce, monkeypatch, capsys):
    """The Railway verification checklist greps for the exact prefix —
    every branch must start with '[emunel] storage :'."""
    for env, ismount in (
        ({"RAILWAY_VOLUME_MOUNT_PATH": "/data"}, False),
        ({"RAILWAY_VOLUME_MOUNT_PATH": "/app/data"}, False),
        ({}, False),
        ({}, True),
    ):
        out = _run(announce, monkeypatch, capsys, env=env, ismount=ismount)
        assert out.startswith("[emunel] storage :"), (env, out)
