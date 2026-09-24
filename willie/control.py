"""Local control socket of the core (F/G): the one door to the robot's body.

The core process owns the MCU link, safety, motion and mood. Other processes on the Pi
(the voice process, bench scripts, the MQTT bridge) ask it through a Unix socket with
one JSON object per line, and get one JSON object back. Robot-local, so no MQTT (D14).

    {"cmd": "state"}                      -> pose, battery, estop, ToF, mood, busy
    {"cmd": "move", "m": 0.3}             -> {"ok": true, "afstand_mm": 301}
    {"cmd": "turn", "deg": -90}
    {"cmd": "stop"} | {"cmd": "clear"} | {"cmd": "look", "pan": 30, "tilt": 10}
    {"cmd": "event", "name": "wake"}      -> mood event from another process
    {"cmd": "mood"}                       -> values + the AI context line + resting face

CLI (on the Pi, with the core running):
    .venv/bin/python -m willie.control state
    .venv/bin/python -m willie.control move 0.2
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

from willie import log as wlog

log = logging.getLogger("willie.control")

SOCKET = wlog.DATA_DIR / "control.sock"
MAX_LINE = 4096


class Body:
    """What the socket commands act on: link + safety + motion + mood, as built by core."""

    def __init__(self, link, safety, motion, mood):
        self.link, self.safety, self.motion, self.mood = link, safety, motion, mood

    def state(self) -> dict:
        st = self.link.state or {}
        age = time.perf_counter() - self.link.state_t if st else None
        return {
            "link": bool(self.link.stats.connected),
            "state_age_s": None if age is None else round(age, 3),
            "pose": {"x_mm": st.get("x_mm"), "y_mm": st.get("y_mm"),
                     "th_deg": None if "th_mrad" not in st else round(st["th_mrad"] / 17.4533, 1)},
            "tof_mm": [st.get("tof_l"), st.get("tof_c"), st.get("tof_r")],
            "estop": st.get("estop", []),
            "battery_v": None if not st.get("mv") else st["mv"] / 1000,
            "battery": self.safety.battery_state,
            "busy": self.motion.busy,
            "mood": self.mood.snapshot(),
        }

    async def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "state":
            return self.state()
        if cmd == "move":
            return await self.motion.move(float(req["m"]), req.get("speed"))
        if cmd == "turn":
            return await self.motion.turn(float(req["deg"]), req.get("speed"))
        if cmd == "stop":
            self.motion.stop()
            return {"ok": True}
        if cmd == "clear":
            self.link.send("clear")
            return {"ok": True}
        if cmd == "look":
            self.link.look(float(req.get("pan", 0)), float(req.get("tilt", 0)))
            return {"ok": True}
        if cmd == "event":
            return {"ok": self.mood.event(str(req.get("name", "")))}
        if cmd == "mood":
            return {"values": self.mood.snapshot(), "context": self.mood.context(), "face": self.mood.face()}
        return {"fout": f"onbekend commando {cmd!r}"}


async def serve(body: Body, path=SOCKET):
    async def client(reader, writer):
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line[:MAX_LINE])
                    reply = await body.handle(req if isinstance(req, dict) else {})
                except (ValueError, KeyError, TypeError) as exc:
                    reply = {"fout": f"verkeerd verzoek: {exc}"}
                except ConnectionError as exc:
                    reply = {"fout": f"geen verbinding met de motorbesturing: {exc}"}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = await asyncio.start_unix_server(client, path=str(path))
    os.chmod(path, 0o660)
    log.info("control socket %s", path)
    async with server:
        await server.serve_forever()


def request(cmd: str, timeout: float = 30.0, path=SOCKET, **args) -> dict:
    """Blocking client, for tool handlers in another process (they run in threads)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(json.dumps({"cmd": cmd, **args}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)
    except (OSError, ValueError) as exc:
        return {"fout": f"het lichaam (willie.service) antwoordt niet: {exc}"}


def main():
    args = sys.argv[1:] or ["state"]
    cmd, rest = args[0], args[1:]
    keys = {"move": ["m"], "turn": ["deg"], "look": ["pan", "tilt"], "event": ["name"]}.get(cmd, [])
    values = {k: (v if k == "name" else float(v)) for k, v in zip(keys, rest)}
    print(json.dumps(request(cmd, **values), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
