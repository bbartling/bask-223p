"""Reference phase-2 live BACnet / baskStream ingest service."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from examples/live-ingest without installing root bask-223p.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if (_REPO_ROOT / "bask_223p").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

__all__ = ["__version__"]
__version__ = "0.1.0"
