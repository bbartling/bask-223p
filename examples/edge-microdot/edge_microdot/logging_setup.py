"""Stdout + tmpfs-friendly rotating logs."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def configure_logging() -> None:
    level = os.environ.get("EDGE_LOG_LEVEL", "INFO").upper()
    log_path = os.environ.get("EDGE_LOG_FILE", "/tmp/edge-microdot.log")
    max_bytes = int(os.environ.get("EDGE_LOG_MAX_BYTES", str(256 * 1024)))
    backup = int(os.environ.get("EDGE_LOG_BACKUP_COUNT", "2"))

    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup, delay=True
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass