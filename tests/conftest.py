"""Per-test-suite path bootstrap: core, worker, console and engines packages."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """Every test starts with an empty rate-limit window.

    The whole suite runs in seconds from a single client identity, which the
    per-IP sliding windows would otherwise trip mid-suite (shared process
    state — exactly what production isolation relies on)."""
    try:
        from emunel_console.security.ratelimit import limiter

        limiter.reset()
    except Exception:
        pass
    yield
    try:
        from emunel_console.security.ratelimit import limiter

        limiter.reset()
    except Exception:
        pass
