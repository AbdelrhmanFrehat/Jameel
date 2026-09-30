"""Structured, secret-free logging for the bridge.

Format:  12:42:10 INFO  DEVICE CONNECTED claude-remote firmware=1.0.0
Console + optional rotating file. A ring buffer keeps the last N records so
GET /log can show them without reading the file.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import threading
from typing import Optional

from bridge.text_utils import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact(text)


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self.buffer: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        with self._lock2:
            self.buffer.append(line)

    def tail(self, n: int = 100) -> list[str]:
        with self._lock2:
            items = list(self.buffer)
        return items[-n:]


_ring: Optional[RingBufferHandler] = None


def setup_logging(level: str = "INFO", log_file: Optional[str] = None, project_root: str = "",
                  max_bytes: int = 2_000_000, backup_count: int = 3,
                  verbose: bool = False) -> RingBufferHandler:
    global _ring
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    lvl = logging.DEBUG if verbose else getattr(logging, level.upper(), logging.INFO)
    root.setLevel(lvl)

    fmt = RedactingFormatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        path = log_file if os.path.isabs(log_file) else os.path.join(project_root, log_file)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes,
                                                      backupCount=backup_count, encoding="utf-8")
            fh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-5s %(name)s %(message)s"))
            root.addHandler(fh)
        except OSError as e:
            root.warning("Could not open log file %s: %s", path, e)

    _ring = RingBufferHandler()
    _ring.setFormatter(fmt)
    root.addHandler(_ring)
    # Quiet the noisy stdlib HTTP server logger unless debugging.
    logging.getLogger("http.server").setLevel(logging.WARNING)
    return _ring


def ring() -> Optional[RingBufferHandler]:
    return _ring
