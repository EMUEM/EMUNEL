"""EMUNEL Console API version info."""
from __future__ import annotations

import os
import re
from pathlib import Path

APP_NAME = "EMUNEL Console"


def version() -> str:
    return os.environ.get("EMUNEL_CONSOLE_VERSION", "1.4.0")


def build() -> str:
    """Deployment build identifier.

    Resolution order: EMUNEL_CONSOLE_BUILD env var > the build stamp file
    written by the Docker image (``/app/.emunel_build``) > ``"dev"``.
    Surfaced in /health, /version and the panel sidebar so a stale
    deployment is recognizable at a glance.
    """
    val = os.environ.get("EMUNEL_CONSOLE_BUILD", "").strip()
    if not val:
        try:
            stamp = Path(__file__).resolve().parents[3] / ".emunel_build"
            val = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            val = ""
    val = re.sub(r"[^A-Za-z0-9 .:+_-]", "", val)
    return val[:48] or "dev"


def info() -> dict:
    return {
        "name": APP_NAME,
        "version": version(),
        "build": build(),
    }
