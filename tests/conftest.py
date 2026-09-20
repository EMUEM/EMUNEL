"""Per-test-suite path bootstrap: core, worker, console and engines packages."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for rel in ("core", "worker", "console/api"):
    sys.path.insert(0, str(ROOT / rel))
