"""Per-test-suite path bootstrap: core, worker, and console packages."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))
