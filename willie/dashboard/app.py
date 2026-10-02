"""Dashboard (A6, on demand since A8/D20): status, log viewer, settings editor generated from the schema.

Runs as its own process (`python -m willie.dashboard`). It talks to the core only through files:
settings YAML (read/write, the core hot-reloads it), the core's log file and state.json.
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from willie import __version__
from willie import log as wlog
from willie.config import Config, ConfigError

STATIC = Path(__file__).parent / "static"
STATE_FILE = wlog.DATA_DIR / "state.json"
last_request = time.monotonic()   # read by __main__ for the idle exit


def tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n") for l in collections.deque(f, maxlen=n)]
    except OSError:
        return [f"(no log file yet at {path})"]


def core_state() -> dict | None:
    try:
        st = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None
    st["age_s"] = round(time.time() - st.get("time", 0), 1)
    st["alive"] = st["age_s"] < 10
    return st


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
    """Pi only: 0x0 = never under-voltage / throttled (used again in B14)."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=1)
        return r.stdout.strip().split("=")[-1] or None
    except Exception:
        return None


def _mjpeg():
    """rpicam-vid MJPEG -> multipart frames. Frames are SOI (FFD8) ... EOI (FFD9), as in remote.py."""
    proc = subprocess.Popen(
        ["rpicam-vid", "--nopreview", "-t", "0", "--codec", "mjpeg", "-q", "50", "--width", "640",
         "--height", "480", "--framerate", "10", "--autofocus-mode", "continuous", "--flush", "-o", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    buffer = b""
    try:
        while chunk := proc.stdout.read(65536):
            buffer += chunk
            while (start := buffer.find(b"\xff\xd8")) >= 0 and (end := buffer.find(b"\xff\xd9", start + 2)) >= 0:
                frame, buffer = buffer[start:end + 2], buffer[end + 2:]
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(frame), frame)
            if len(buffer) > 2_000_000:
                buffer = b""
    finally:                                    # client left (generator closed) or camera ended
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="WILL-E", version=__version__, docs_url="/api/docs", redoc_url=None)
    proc = psutil.Process(os.getpid())
    psutil.cpu_percent(None)  # prime the counter

    @app.middleware("http")
    async def touch(request, call_next):
        global last_request
        last_request = time.monotonic()
        return await call_next(request)

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
            "cpu_percent": psutil.cpu_percent(None),
            "load": [round(x, 2) for x in la],
            "ram_total_mb": round(vm.total / 2**20),
            "ram_available_mb": round(vm.available / 2**20),
            "dashboard_rss_mb": round(proc.memory_info().rss / 2**20, 1),
            "core": core_state(),
            "temp_c": cpu_temp_c(),
            "throttled": throttled(),
            "wifi": wifi(),
        }

    @app.get("/api/logs")
    def logs(n: int | None = None):
        n = max(1, min(n or cfg.get("dashboard.log_lines"), 2000))
        cfg.reload()
        return {"lines": tail(wlog.LOG_FILE, n)}

    @app.get("/api/schema")
    def schema():
        return cfg.schema

    @app.get("/api/config")
    def get_config():
        cfg.reload()
        return {"path": str(cfg.path), "values": cfg.snapshot()}

    @app.put("/api/config")
    def put_config(changes: dict):
        try:
            return {"values": cfg.update(changes)}
        except ConfigError as e:
            raise HTTPException(status_code=422, detail=e.errors)

    # Debug tab (bench only): wheel power by hand. The core arms it for 10 minutes, clamps to drive.pwm_cap and the
    # firmware watchdog stops the wheels 200 ms after the page stops sending (willie/control.py _debug).
    from willie import control

    @app.get("/api/debug/state")
    def debug_state():
        return control.request("debug_state", timeout=1.0)

    @app.post("/api/debug/arm")
    def debug_arm(body: dict):
        return control.request("debug_arm", timeout=1.0, on=bool(body.get("on")))

    @app.post("/api/debug/drive")
    def debug_drive(body: dict):
        try:
            left, right = float(body.get("l", 0)), float(body.get("r", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="l and r must be numbers")
        return control.request("debug_drive", timeout=0.5, l=left, r=right)

    @app.post("/api/debug/clear")
    def debug_clear():
        return control.request("clear", timeout=0.5)

    @app.post("/api/debug/stop")
    def debug_stop():
        return control.request("stop", timeout=0.5)

    # Camera tab (F4): WASD driving + live view. Armed by hand for 10 min; the page sends fractions of the speed
    # limits at 10 Hz, the core gates them (Safety.gate) and the firmware watchdog stops the wheels when it stops.
    @app.get("/api/manual/state")
    def manual_state():
        return control.request("manual_state", timeout=1.0)

    @app.post("/api/manual/arm")
    def manual_arm(body: dict):
        return control.request("manual_arm", timeout=1.0, on=bool(body.get("on")))

    @app.post("/api/manual/drive")
    def manual_drive(body: dict):
        try:
            v, w = float(body.get("v", 0)), float(body.get("w", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="v and w must be numbers")
        return control.request("manual_drive", timeout=0.5, v=v, w=w)

    @app.get("/api/camera/stream")
    def camera_stream():
        """MJPEG for <img>. rpicam-vid runs only while this response is open (D20: on demand)."""
        if not shutil.which("rpicam-vid"):
            raise HTTPException(status_code=503, detail="rpicam-vid missing")
        return StreamingResponse(_mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame")

    # H1: skills on/off. Stored in skills.disabled; the voice process refuses a switched-off
    # skill's tools at once and leaves them out of the next conversation's tool list.
    @app.get("/api/skills")
    def list_skills():
        from willie import skills
        return {"skills": skills.info()}

    @app.put("/api/skills/{name}")
    def toggle_skill(name: str, body: dict):
        from willie import skills
        try:
            skills.set_enabled(name, bool(body.get("enabled")))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no skill {name}")
        cfg.reload()
        return {"skills": skills.info()}

    return app
