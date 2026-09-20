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


async def write_state(link, every_s: float = 2.0):
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
        }
        tmp.write_text(json.dumps(state))
        os.replace(tmp, STATE_FILE)
        await asyncio.sleep(every_s)


async def amain():
    wlog.setup()
    cfg = Config()
    log.info("WILL-E %s starting (settings: %s)", __version__, cfg.path)
    await hello.run()

    from willie.hal.link import Link  # pyserial only; tiny

    link = Link(cfg.get("link.port"), cfg.get("link.baud"), cfg.get("link.ping_hz"))
    cfg.on_change(lambda old, new: setattr(link, "ping_hz", new["link"]["ping_hz"]))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(supervise("config-watch", lambda: watch_config(cfg))),
        asyncio.create_task(supervise("link", link.run)),
        asyncio.create_task(supervise("state", lambda: write_state(link))),
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
