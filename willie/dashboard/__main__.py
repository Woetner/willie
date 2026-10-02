"""Run the dashboard as its own, short-lived process (D20).

On the Pi, systemd owns port 8080 (willie-dashboard.socket) and starts this with --fd 3
on the first connection. It exits after `dashboard.idle_minutes` without requests;
the next browser visit starts it again (takes a few seconds on the Pi 3 A+).

On the Mac:  python -m willie.dashboard --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

import uvicorn

from willie.config import Config
from willie.dashboard import app as appmod

log = logging.getLogger("willie.dashboard")


async def idle_exit(servers: list, cfg: Config):
    while not servers[0].should_exit:
        await asyncio.sleep(10)
        idle_s = time.monotonic() - appmod.last_request
        if idle_s > cfg.get("dashboard.idle_minutes") * 60:
            log.info("dashboard idle for %.0f s -> exiting (systemd keeps the ports open)", idle_s)
            for server in servers:
                server.should_exit = True


async def amain(args):
    cfg = Config()
    app = appmod.create_app(cfg)

    def make(**kw):
        return uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, **kw))

    servers = [make(fd=args.fd) if args.fd is not None else make(host=args.host, port=args.port)]
    # The same app over HTTPS, in this process (one camera stream owner, one idle timer): systemd passes the second
    # socket as fd 4; on the Mac use --tls-port.
    if (args.tls_fd is not None or args.tls_port) and args.cert and args.key:
        if Path(args.cert).exists() and Path(args.key).exists():
            where = {"fd": args.tls_fd} if args.tls_fd is not None else {"host": args.host, "port": args.tls_port}
            servers.append(make(ssl_certfile=args.cert, ssl_keyfile=args.key, **where))
        else:
            log.warning("no certificate at %s: HTTPS is off (run tools/install_tls.sh)", args.cert)
    watcher = asyncio.create_task(idle_exit(servers, cfg))
    await asyncio.gather(*(server.serve() for server in servers))
    watcher.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, help="listening socket passed by systemd")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tls-fd", type=int, help="HTTPS listening socket passed by systemd")
    ap.add_argument("--tls-port", type=int, default=0, help="HTTPS port when not run by systemd")
    ap.add_argument("--cert", help="certificate (PEM) for HTTPS")
    ap.add_argument("--key", help="private key (PEM) for HTTPS")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    asyncio.run(amain(args))


main()
