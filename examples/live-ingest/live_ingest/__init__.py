"""Reference phase-2 live BACnet / baskStream ingest service."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from examples/live-ingest without installing root bask-223p.
_SRC = Path(__file__).resolve().parents[3] / "src"
if (_SRC / "bask_223p").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

__all__ = ["__version__"]
__version__ = "0.1.0"
