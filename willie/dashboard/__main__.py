"""Run the dashboard as its own process (D20).

On the Pi, systemd owns port 8080 (willie-dashboard.socket) and starts this at boot with
--fd 3 --always-on. Without --always-on it exits after `dashboard.idle_minutes` without
requests and the next browser visit starts it again (a few seconds on the Pi 3 A+).

On the Mac:  python -m willie.dashboard --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

import uvicorn

from willie.config import Config
from willie.dashboard import app as appmod

log = logging.getLogger("willie.dashboard")


async def idle_exit(server: uvicorn.Server, cfg: Config):
    while not server.should_exit:
        await asyncio.sleep(10)
        idle_s = time.monotonic() - appmod.last_request
        if idle_s > cfg.get("dashboard.idle_minutes") * 60:
            log.info("dashboard idle for %.0f s -> exiting (systemd keeps port 8080 open)", idle_s)
            server.should_exit = True


async def amain(args):
    cfg = Config()
    kw = {"fd": args.fd} if args.fd is not None else {"host": args.host, "port": args.port}
    server = uvicorn.Server(uvicorn.Config(
        appmod.create_app(cfg), log_config=None, access_log=False, **kw))
    watcher = None if args.always_on else asyncio.create_task(idle_exit(server, cfg))
    await server.serve()
    if watcher:
        watcher.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, help="listening socket passed by systemd")
    ap.add_argument("--always-on", action="store_true", help="never exit when idle")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    asyncio.run(amain(args))


main()
