"""A6 dashboard skeleton: status, log viewer, settings editor generated from the schema."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from willie import __version__
from willie import log as wlog
from willie.config import Config, ConfigError

STATIC = Path(__file__).parent / "static"
_BOOT = time.time()


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpu_temp_c() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    return round(int(raw) / 1000, 1) if raw and raw.isdigit() else None


def wifi() -> dict:
    """Signal from /proc/net/wireless, SSID from iwgetid if present (no extra packages)."""
    out = {"iface": None, "ssid": None, "signal_dbm": None, "quality": None}
    raw = _read("/proc/net/wireless")
    if raw:
        for line in raw.splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 4:
                out["iface"] = parts[0].rstrip(":")
                out["quality"] = float(parts[2].rstrip("."))
                out["signal_dbm"] = float(parts[3].rstrip("."))
                break
    if shutil.which("iwgetid"):
        try:
            out["ssid"] = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=1).stdout.strip() or None
        except Exception:
            pass
    return out


def throttled() -> str | None:
    """Pi only: 0x0 = never under-voltage / throttled (used again in B13)."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=1)
        return r.stdout.strip().split("=")[-1] or None
    except Exception:
        return None


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="WILL-E", version=__version__, docs_url="/api/docs", redoc_url=None)
    proc = psutil.Process(os.getpid())
    psutil.cpu_percent(None)  # prime the counter

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        vm = psutil.virtual_memory()
        la = os.getloadavg()
        return {
            "version": __version__,
            "host": socket.gethostname(),
            "uptime_s": int(time.time() - psutil.boot_time()),
            "willie_uptime_s": int(time.time() - _BOOT),
            "cpu_percent": psutil.cpu_percent(None),
            "load": [round(x, 2) for x in la],
            "ram_total_mb": round(vm.total / 2**20),
            "ram_available_mb": round(vm.available / 2**20),
            "willie_rss_mb": round(proc.memory_info().rss / 2**20, 1),
            "temp_c": cpu_temp_c(),
            "throttled": throttled(),
            "wifi": wifi(),
        }

    @app.get("/api/logs")
    def logs(n: int | None = None):
        n = n or cfg.get("dashboard.log_lines")
        lines = list(wlog.ring.lines)
        return {"lines": lines[-max(1, min(n, 2000)):]}

    @app.get("/api/schema")
    def schema():
        return cfg.schema

    @app.get("/api/config")
    def get_config():
        return {"path": str(cfg.path), "values": cfg.snapshot()}

    @app.put("/api/config")
    def put_config(changes: dict):
        try:
            return {"values": cfg.update(changes)}
        except ConfigError as e:
            raise HTTPException(status_code=422, detail=e.errors)

    return app
