"""Logging: stdout (-> journald) and a rotating file (read by the on-demand dashboard)."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("WILLIE_DATA", Path.home() / ".local" / "share" / "willie"))
LOG_FILE = DATA_DIR / "willie.log"
FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


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
    for h in (stream, filed):
        root.addHandler(h)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # keep the log readable
