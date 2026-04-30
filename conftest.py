"""Make the repo root importable so `from retailers.base import ...` works in tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
