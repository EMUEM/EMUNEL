#!/usr/bin/env python3
"""EMUNEL Engine Manager — operator CLI (repo root).

Runs entirely offline: reads env + engine state files, never starts
servers. Use it to inspect what the engines will do (or did) on the
current machine/deployment.

    python engine_manager.py status     # per-engine env/activation matrix
    python engine_manager.py list       # registry names + hosts
    python engine_manager.py doctor     # data dir, volume probe, env audit
    python engine_manager.py selftest   # codec + pipeline unit checks

Exit code 0 = healthy/operational, 1 = warnings, 2 = failure.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engines.config import get_env                      # noqa: E402
from engines.engines import REGISTRY                   # noqa: E402
from engines.state import EngineStateStore             # noqa: E402


def _engine_names() -> list[str]:
    env = get_env("cli")
    order = [n for n in env.pipeline_order]
    for name in REGISTRY:
        if name not in order:
            order.append(name)
    return order


def _enabled_reason(name: str, env) -> str:
    key = f"{name.lower()}_on"
    if hasattr(env, key) and not getattr(env, key):
        return "off (env)"
    if name not in (env.pipeline_order or []):
        return "off (not in pipeline)"
    return "on"


def cmd_status(json_mode: bool = False) -> int:
    env = get_env("cli")
    state = EngineStateStore(env.data_dir)
    state.load()
    saved = state.get("__saved__")
    engines = []
    for name in _engine_names():
        cls = REGISTRY[name]
        payload = state.get(name, {}) if hasattr(state, "get") else {}
        engines.append({
            "name": name,
            "title": cls.TITLE,
            "hosts": sorted(cls.HOSTS),
            "pipeline": name in (env.pipeline_order or []),
            "enabled": _enabled_reason(name, env),
            "saved_state_keys": sorted(payload.keys())[:8],
        })
    out = {
        "engines_enabled_system": env.enabled,
        "pipeline_order": env.pipeline_order,
        "data_dir": env.data_dir,
        "volume_warning": state.volume_warning,
        "engines": engines,
    }
    if json_mode:
        print(json.dumps(out, indent=2))
        return 0
    print(f"engines system      : {'ENABLED' if env.enabled else 'DISABLED'}")
    print(f"pipeline order      : {', '.join(env.pipeline_order) or '(empty)'}")
    print(f"engine data dir     : {env.data_dir}")
    if state.volume_warning:
        print(f"!! volume warning   : {state.volume_warning}")
    print()
    for e in engines:
        mark = "ACTIVE  " if e["pipeline"] and e["enabled"] == "on" else e["enabled"]
        print(f"  [{mark:>8}] {e['name']:<18} hosts={','.join(e['hosts']):<16} {e['title']}")
    return 1 if state.volume_warning else 0


def cmd_list() -> int:
    for name in _engine_names():
        cls = REGISTRY[name]
        print(f"{name:<18} hosts={','.join(sorted(cls.HOSTS)):<16} {cls.TITLE}")
    return 0


def cmd_doctor() -> int:
    env = get_env("cli")
    problems: list[str] = []
    state = EngineStateStore(env.data_dir)
    # data dir writability
    try:
        probe = Path(env.data_dir) / ".cli-write-probe"
        probe.write_text("ok")
        probe.unlink()
        print(f"OK   data dir writable: {env.data_dir}")
    except OSError as exc:
        problems.append(f"data dir not writable: {exc}")
        print(f"FAIL data dir not writable: {env.data_dir} ({exc})")
    # persistence
    if state.volume_warning:
        problems.append(state.volume_warning)
        print(f"WARN {state.volume_warning}")
    else:
        print("OK   engine data persisted (volume probe token survived)")
    # pipeline sanity
    unknown = [n for n in env.pipeline_order if n not in REGISTRY]
    if unknown:
        problems.append(f"unknown engines in pipeline: {unknown}")
        print(f"FAIL unknown engines in pipeline: {unknown}")
    else:
        print(f"OK   pipeline order valid: {', '.join(env.pipeline_order) or '(empty)'}")
    # core-side engines need the engines package on the subprocess path
    if os.environ.get("EMUNEL_CORE_MODULE") == "engines.core_host":
        print("OK   EMUNEL_CORE_MODULE=engines.core_host (core-side engines on)")
    else:
        print("note EMUNEL_CORE_MODULE unset — Cores launch without engines "
              "(set automatically when core-side engines are active)")
    print(f"note PORT={os.environ.get('PORT', '(unset — uvicorn default 8080)')}")
    return 2 if any(p.startswith("FAIL") for p in problems) else (1 if problems else 0)


def cmd_selftest() -> int:
    results: dict[str, bool | str] = {}
    try:
        from engines.engines.fec import decode_group, encode_group

        data = [b"alpha", b"beta", b"gamma", b"delta"]
        enc = encode_group(data, parity_count=1)
        holes = list(enc)
        holes[2] = None
        rec = decode_group(holes, parity_count=1)
        results["fec_codec"] = bool(rec and rec[2][:5] == b"gamma")
    except Exception as exc:
        results["fec_codec"] = f"error: {exc}"
    try:
        import zlib

        blob = b"x" * 4096
        results["zlib_roundtrip"] = zlib.decompress(zlib.compress(blob)) == blob
    except Exception as exc:
        results["zlib_roundtrip"] = f"error: {exc}"
    try:
        from engines.configgen_util import raw_decode, raw_encode, rewrite_url

        url = "vless://uuid@example.com:443?security=tls&sni=example.com&path=%2Fi%2Ft%2Fws%2Fu&type=ws#lbl"
        new = rewrite_url(url, host="alt.example.org", sni="alt.example.org")
        results["url_rewrite"] = ("alt.example.org" in new
                                  and "sni=alt.example.org" in new)
        body = raw_encode([url])
        results["raw_roundtrip"] = raw_decode(body) == [url]
    except Exception as exc:
        results["url_rewrite"] = f"error: {exc}"
    try:
        from engines.profiles import LinUCB

        bandit = LinUCB(alpha=0.3)
        ctx = LinUCB.context()
        name1, _ = bandit.choose(["a", "b"], ctx)
        bandit.update(name1, ctx, 1.0)
        results["linucb"] = bandit.arms[name1].pulls == 1
    except Exception as exc:
        results["linucb"] = f"error: {exc}"
    ok = True
    for key, value in results.items():
        passed = value is True
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {key}: {value}")
    return 0 if ok else 2


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command in ("--json", "status"):
        return cmd_status(json_mode=(command == "--json"))
    if command == "list":
        return cmd_list()
    if command == "doctor":
        return cmd_doctor()
    if command == "selftest":
        return cmd_selftest()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
