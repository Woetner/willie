"""Logging: stdout (-> journald), a rotating file, and an in-memory ring for the dashboard."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("WILLIE_DATA", Path.home() / ".local" / "share" / "willie"))
LOG_FILE = DATA_DIR / "willie.log"
FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingHandler(logging.Handler):
    def __init__(self, size: int = 2000):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=size)

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


ring = RingHandler()


def setup(level: str = "INFO"):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(FMT, "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))  # journald adds time
    filed = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    filed.setFormatter(fmt)
    ring.setFormatter(fmt)
    for h in (stream, filed, ring):
        root.addHandler(h)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # keep the log readable
