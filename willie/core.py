"""Start-up and task supervisor (one process, asyncio)."""
from __future__ import annotations

import asyncio
import logging
import signal

from willie import __version__, hello
from willie import log as wlog
from willie.config import Config

log = logging.getLogger("willie.core")


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
    """Hot reload: pick up hand edits of the live YAML file."""
    while True:
        await asyncio.sleep(every_s)
        cfg.reload()


async def amain():
    wlog.setup()
    cfg = Config()
    log.info("WILL-E %s starting (settings: %s)", __version__, cfg.path)
    await hello.run()

    import uvicorn  # imported late: only needed once logging is up

    from willie.dashboard.app import create_app

    port = cfg.get("dashboard.port")
    server = uvicorn.Server(uvicorn.Config(
        create_app(cfg), host="0.0.0.0", port=port, log_config=None, access_log=False,
    ))
    server.install_signal_handlers = lambda: None  # we handle signals ourselves

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(supervise("dashboard", server.serve)),
        asyncio.create_task(supervise("config-watch", lambda: watch_config(cfg))),
    ]
    log.info("dashboard on http://willie.local:%d", port)
    await stop.wait()
    log.info("stopping")
    server.should_exit = True
    await asyncio.sleep(0.5)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main():
    asyncio.run(amain())
