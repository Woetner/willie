"""Start-up and task supervisor (one always-on process, asyncio).

The dashboard is NOT in here any more (D20): it is a separate on-demand process
(`python -m willie.dashboard`, started by systemd socket activation) that reads the
live settings file, the log file and the state file this process writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import signal
import time

from willie import __version__, hello
from willie import log as wlog
from willie.config import Config

log = logging.getLogger("willie.core")

STATE_FILE = wlog.DATA_DIR / "state.json"
_START = time.time()


async def supervise(name: str, coro_fn, restart_s: float = 5.0):
    """Run a long-lived task; log and restart it if it crashes."""
    while True:
        try:
            await coro_fn()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("task %s crashed, restarting in %.0f s", name, restart_s)
            await asyncio.sleep(restart_s)


async def watch_config(cfg: Config, every_s: float = 2.0):
    """Hot reload: pick up changes from the dashboard or hand edits of the live YAML file."""
    while True:
        await asyncio.sleep(every_s)
        cfg.reload()


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    # macOS (run-local): ru_maxrss is bytes there
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)


async def write_state(link, body=None, every_s: float = 2.0):
    """Small JSON snapshot for the dashboard (separate process)."""
    tmp = STATE_FILE.with_suffix(".tmp")
    while True:
        state = {
            "version": __version__,
            "pid": os.getpid(),
            "time": time.time(),
            "uptime_s": int(time.time() - _START),
            "rss_mb": _rss_mb(),
            "link": link.stats.summary() if link else None,
            "body": body.state() if body else None,
        }
        tmp.write_text(json.dumps(state))
        os.replace(tmp, STATE_FILE)
        await asyncio.sleep(every_s)


ESTOP_MOOD = {"bump_l": "bumper.hit", "bump_r": "bumper.hit", "cliff_l": "cliff", "cliff_r": "cliff"}


def on_mcu_message(words, motion, mood, link):
    """MCU events (§5.2). An estop has already braked the motors in the firmware."""
    if words[0] == "estop" and len(words) >= 2:
        reason = words[1]
        log.warning("MCU estop: %s (while %s)", reason, motion.busy or "idle")
        mood.event(ESTOP_MOOD.get(reason, "error"))
        if motion.busy in ("move", "turn"):
            asyncio.ensure_future(motion.recover(reason))       # F1: clear + back off 5 cm
        elif reason.startswith("bump"):
            link.send("clear")      # standing still and touched (a foot, the cat): nothing to undo
        # cliff/tilt while idle stay latched until someone sends `clear` (control socket)
    elif words[0] == "err" and words[1:2] != ["estop"]:
        log.info("MCU: %s", " ".join(words))


def safe_poweroff():
    """Battery under cutoff for 10 s (F1): a clean shutdown protects the SD card and cells."""
    log.critical("battery empty: powering off")
    import subprocess
    subprocess.Popen(["sudo", "-n", "systemctl", "poweroff"])


async def body_loop(link, safety, mood, hz: float = 5.0):
    """Battery rules and mood drift at 5 Hz (the MCU streams at 50 Hz; that is plenty)."""
    while True:
        safety.battery(link.state)
        mood.update()
        await asyncio.sleep(1 / hz)


async def amain():
    wlog.setup()
    cfg = Config()
    log.info("WILL-E %s starting (settings: %s)", __version__, cfg.path)
    await hello.run()

    from willie.hal.link import Link  # pyserial only; tiny

    from willie.hal.link import mcu_settings

    link = Link(cfg.get("link.port"), cfg.get("link.baud"), cfg.get("link.ping_hz"))

    def push_mcu_settings():
        if link.stats.connected:
            for key, value in mcu_settings(cfg.get).items():
                link.cfg(key, value)

    link.on_hello = push_mcu_settings        # the firmware boots with its own defaults

    # F1/G2: safety gate, motion primitives, mood; reached through the control socket.
    from willie import control
    from willie.behavior.mood import Mood
    from willie.motion import Motion
    from willie.safety import Safety

    safety = Safety(cfg.get, on_poweroff=safe_poweroff, on_event=lambda name, detail: mood.event(name))
    motion = Motion(link, safety, cfg.get)
    mood = Mood(cfg.get)
    body = control.Body(link, safety, motion, mood)
    link.on_message = lambda words: on_mcu_message(words, motion, mood, link)
    cfg.on_change(lambda old, new: setattr(link, "ping_hz", new["link"]["ping_hz"]))
    cfg.on_change(lambda old, new: push_mcu_settings())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(supervise("config-watch", lambda: watch_config(cfg))),
        asyncio.create_task(supervise("link", link.run)),
        asyncio.create_task(supervise("state", lambda: write_state(link, body))),
        asyncio.create_task(supervise("body", lambda: body_loop(link, safety, mood))),
        asyncio.create_task(supervise("control", lambda: control.serve(body))),
    ]
    log.info("core running, RSS %.1f MB", _rss_mb())
    await stop.wait()
    log.info("stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    link.close()


def main():
    asyncio.run(amain())
