"""Local control socket of the core (F/G): the one door to the robot's body.

The core process owns the MCU link, safety, motion and mood. Other processes on the Pi
(the voice process, bench scripts, the MQTT bridge) ask it through a Unix socket with
one JSON object per line, and get one JSON object back. Robot-local, so no MQTT (D14).

    {"cmd": "state"}                      -> pose, battery, estop, ToF, mood, busy
    {"cmd": "move", "m": 0.3}             -> {"ok": true, "afstand_mm": 301}
    {"cmd": "turn", "deg": -90}
    {"cmd": "stop"} | {"cmd": "clear"} | {"cmd": "look", "pan": 30, "tilt": 10}
    {"cmd": "event", "name": "wake"}      -> mood event from another process
    {"cmd": "event", "name": "mission_start"|"mission_end"}   a Phase K mission owns the wheels
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
from willie.safety import MAX_TURN

log = logging.getLogger("willie.control")

SOCKET = wlog.DATA_DIR / "control.sock"
MAX_LINE = 4096


DEBUG_ARM_S = 600.0     # the Debug tab's motor control stays armed this long, then it must be armed again
MANUAL_ARM_S = 600.0    # same for the Camera tab's WASD driving (F4)
MANUAL_HOLD_S = 0.6     # keys count as held this long after the last drive command (behaviours stay put)


class Body:
    """What the socket commands act on: link + safety + motion + mood, as built by core."""

    def __init__(self, link, safety, motion, mood):
        self.link, self.safety, self.motion, self.mood = link, safety, motion, mood
        self.conversation = False       # a voice session is open: behaviours sit still (G3)
        self.mission = False            # a Phase K mission drives him: behaviours sit still
        self.last_command = 0.0         # monotonic time of the last move/turn from outside
        self.behaviours = None          # behavior.tree.Behaviours, set by core
        self._power = None              # willie.power.PowerEstimator, built on first use
        self.debug_armed_until = 0.0    # monotonic: the dashboard's Debug tab may drive the wheels until then
        self.debug_active_until = 0.0   # monotonic: a debug drive command came in this recently
        self.manual_armed_until = 0.0   # monotonic: the Camera tab may drive (WASD) until then

    @property
    def debug_active(self) -> bool:
        return time.monotonic() < self.debug_active_until

    def _debug(self, cmd: str, req: dict) -> dict:
        """Dashboard Debug tab: raw wheel power for bench tests (WILL-E.md §10). Armed by hand for 10 min,
        never by the AI; refreshed at 10 Hz by the page, and the firmware watchdog (200 ms) stops the wheels
        when it stops. Clamped to drive.pwm_cap, refused under the battery cutoff."""
        now = time.monotonic()
        if cmd == "debug_arm":
            if req.get("on"):
                self.debug_armed_until = now + DEBUG_ARM_S
            else:
                self.debug_armed_until = 0.0
                self.link.stop()
            return {"armed": now < self.debug_armed_until, "seconds_left": max(0, round(self.debug_armed_until - now))}
        if cmd == "debug_state":
            st = self.link.state or {}
            keys = ("ms", "ticks_l", "ticks_r", "pwm_l", "pwm_r", "ma", "mv", "tof_l", "tof_c", "tof_r",
                    "tof_cl", "tof_cr", "tilt_d10", "i2c_err")
            return {"link": bool(self.link.stats.connected), "armed": now < self.debug_armed_until,
                    "seconds_left": max(0, round(self.debug_armed_until - now)),
                    "estop": st.get("estop", []), "moving": st.get("moving", False),
                    **{k: st.get(k) for k in keys}}
        # debug_drive
        if now >= self.debug_armed_until:
            return {"fout": "debug drive is not armed: press 'Arm' in the Debug tab first"}
        if self.safety.battery_state == "cutoff":
            return {"fout": "battery empty"}
        cap = float(self.safety.get("drive.pwm_cap"))
        left = max(-cap, min(cap, float(req.get("l", 0))))
        right = max(-cap, min(cap, float(req.get("r", 0))))
        self.debug_active_until = now + 0.6
        latched = set((self.link.state or {}).get("estop", []))
        if latched and latched <= {"cliff_l", "cliff_r"} and not self.safety.get("safety.cliff_stop"):
            self.link.send("clear")             # the switch is off: an old cliff stop must not keep the wheels still
        if left == 0 and right == 0:
            self.link.stop()
        else:
            self.link.pwm(left, right)
        return {"ok": True, "l": left, "r": right}

    def _manual(self, cmd: str, req: dict) -> dict:
        """Camera tab WASD driving (F4): the page sends fractions (-1..1) of drive.max_speed and the turn limit
        at 10 Hz while keys are held. Armed by hand, goes through Safety.gate() like every other motion, and
        the firmware watchdog (200 ms) stops the wheels when the page stops sending."""
        now = time.monotonic()
        left = max(0, round(self.manual_armed_until - now))
        if cmd == "manual_arm":
            if req.get("on"):
                self.manual_armed_until = now + MANUAL_ARM_S
            else:
                self.manual_armed_until = 0.0
                self.link.stop()
            return {"armed": now < self.manual_armed_until, "seconds_left": max(0, round(self.manual_armed_until - now))}
        if cmd == "manual_state":
            return {"armed": now < self.manual_armed_until, "seconds_left": left, "state": self.state()}
        if now >= self.manual_armed_until:
            return {"fout": "manual drive is not armed: press 'Arm' in the Camera tab first"}
        fv = max(-1.0, min(1.0, float(req.get("v", 0))))
        fw = max(-1.0, min(1.0, float(req.get("w", 0))))
        self.last_command = now
        if self.motion.busy:
            self.motion.stop()                  # a driving key beats a running move/turn
        if fv == 0 and fw == 0:
            self.link.stop()
            return {"ok": True, "v": 0, "w": 0}
        verdict = self.safety.gate(fv * float(self.safety.get("drive.max_speed")), fw * MAX_TURN,
                                   self.link.state, time.perf_counter() - self.link.state_t if self.link.state else 1e9)
        if verdict.refused:
            self.link.stop()
            return {"ok": False, "reden": verdict.reason}
        self.link.drive(verdict.v, verdict.w)
        return {"ok": True, "v": verdict.v, "w": verdict.w, "reden": verdict.reason}

    def _power_numbers(self, st: dict) -> dict:
        """Battery %, watts and runtime from the INA219 values (willie/power.py)."""
        if self._power is None:
            from willie.power import PowerEstimator
            get = self.safety.get
            self._power = PowerEstimator(pack_wh=float(get("power.pack_wh")),
                                         r_int_ohm=float(get("power.r_internal_mohm")) / 1000.0,
                                         cutoff_v=float(get("safety.battery_cutoff_v")))
        return self._power.update(st.get("mv"), st.get("ma"), time.monotonic())

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
            **self._power_numbers(st),
            "battery": self.safety.battery_state,
            "busy": self.motion.busy,
            "conversation": self.conversation,
            "mission": self.mission,
            "imu": {"acc_mg": [st.get("ax"), st.get("ay"), st.get("az")]} if st.get("az") is not None else None,
            "behaviour": self.behaviours.current if self.behaviours else None,
            "mood": self.mood.snapshot(),
        }

    async def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "state":
            return self.state()
        if cmd == "move":
            self.last_command = time.monotonic()
            return await self.motion.move(float(req["m"]), req.get("speed"))
        if cmd == "turn":
            self.last_command = time.monotonic()
            return await self.motion.turn(float(req["deg"]), req.get("speed"))
        if cmd in ("debug_arm", "debug_drive", "debug_state"):
            return self._debug(cmd, req)
        if cmd in ("manual_arm", "manual_drive", "manual_state"):
            return self._manual(cmd, req)
        if cmd == "stop":
            self.debug_active_until = 0.0
            self.motion.stop()
            return {"ok": True}
        if cmd == "clear":
            self.link.send("clear")
            return {"ok": True}
        if cmd == "look":
            self.link.look(float(req.get("pan", 0)), float(req.get("tilt", 0)))
            return {"ok": True}
        if cmd == "event":
            name = str(req.get("name", ""))
            if name in ("conversation_start", "conversation_end"):
                self.conversation = name == "conversation_start"
                return {"ok": True}
            if name in ("mission_start", "mission_end"):
                self.mission = name == "mission_start"
                return {"ok": True}
            return {"ok": self.mood.event(name)}
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


def event(name: str) -> None:
    """Fire-and-forget mood event from another process (never blocks the caller; with the
    core down the event is simply lost - mood is a nicety, D22-style)."""
    import threading
    threading.Thread(target=request, args=("event",), kwargs={"timeout": 0.5, "name": name},
                     daemon=True).start()


def context_line(timeout: float = 0.3) -> str:
    """Mood + battery for the AI's live context, or "" when the core does not answer."""
    mood = request("mood", timeout=timeout)
    if "context" not in mood:
        return ""
    state = request("state", timeout=timeout)
    volts = state.get("battery_v")
    line = mood["context"]
    if volts and state.get("battery") != "unknown":
        line += f"; batterij {volts:.1f} V ({state['battery']})"
    return line


def main():
    args = sys.argv[1:] or ["state"]
    cmd, rest = args[0], args[1:]
    keys = {"move": ["m"], "turn": ["deg"], "look": ["pan", "tilt"], "event": ["name"]}.get(cmd, [])
    values = {k: (v if k == "name" else float(v)) for k, v in zip(keys, rest)}
    print(json.dumps(request(cmd, **values), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
