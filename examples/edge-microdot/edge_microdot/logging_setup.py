"""Stdout immediately; file logs drain on an interval so the SD card is not written per line."""
from __future__ import annotations

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

_buffer: "IntervalBuffer | None" = None


class IntervalBuffer(logging.Handler):
    """Hold log records in RAM and hand them to a file handler on drain()."""

    def __init__(self, target: logging.Handler, interval_s: float) -> None:
        super().__init__()
        self.target = target
        self.interval_s = max(0.0, float(interval_s))
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()
        self.last = time.monotonic()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self.records.append(record)

    def drain(self) -> int:
        with self._lock:
            pending = self.records
            self.records = []
            self.last = time.monotonic()
        for rec in pending:
            self.target.handle(rec)
        self.target.flush()
        return len(pending)

    def due(self) -> bool:
        if self.interval_s <= 0:
            return True
        return (time.monotonic() - self.last) >= self.interval_s


def drain_logs(*, force: bool = False) -> int:
    """Write buffered file logs when the interval has elapsed (or on shutdown)."""
    buf = _buffer
    if buf is None:
        return 0
    if not force and not buf.due():
        return 0
    return buf.drain()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def log_flush_seconds() -> float:
    raw = os.environ.get("EDGE_LOG_FLUSH_MINUTES", "").strip()
    if not raw and _truthy("EDGE_SD_CARD"):
        raw = os.environ.get("EDGE_DURABLE_MINUTES", "15")
    if not raw:
        return 0.0
    return max(0.0, float(raw)) * 60.0


def configure_logging() -> None:
    global _buffer
    level = os.environ.get("EDGE_LOG_LEVEL", "INFO").upper()
    log_path = os.environ.get("EDGE_LOG_FILE", "/tmp/edge-microdot.log")
    max_bytes = int(os.environ.get("EDGE_LOG_MAX_BYTES", str(256 * 1024)))
    backup = int(os.environ.get("EDGE_LOG_BACKUP_COUNT", "2"))
    interval_s = log_flush_seconds()

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
    except OSError:
        return

    if interval_s > 0:
        _buffer = IntervalBuffer(file_handler, interval_s)
        _buffer.setFormatter(fmt)
        root.addHandler(_buffer)
    else:
        root.addHandler(file_handler)
