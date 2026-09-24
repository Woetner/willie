"""Motion primitives (F1-F3): move(m), turn(deg), recover() - classic code, no AI (§1.2).

Closed loop on the firmware's odometry (x/y/θ in every `st` frame), commanded at 20 Hz
with `drive v ω` - well inside the firmware's 200 ms watchdog, so if this process stalls
the motors stop by themselves. Every command passes `Safety.gate()` first.

One owner: a new request cancels the running one (the robot never runs two motions at
once, and `stop()` always wins). Results are small dicts in Dutch, ready for a tool reply.

Wheel-speed PID (F2) belongs in the firmware (it needs the encoder timing); until then
the firmware drives open loop and these loops correct distance and heading from odometry.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

log = logging.getLogger("willie.motion")

HZ = 20                  # command rate; 50 ms << 200 ms watchdog
MOVE_TOL_MM = 10
TURN_TOL_RAD = math.radians(2)
CREEP_V = 0.04           # m/s: slowest useful speed (open-loop motors stall below ~this)
CREEP_W = 0.3            # rad/s
HEADING_GAIN = 2.0       # rad/s per rad of heading error while driving straight
RECOVER_V = 0.1          # m/s backwards after a bump or cliff


def wrap(a: float) -> float:
    return math.remainder(a, 2 * math.pi)


def pose(state: dict) -> tuple[float, float, float]:
    return state["x_mm"] / 1000, state["y_mm"] / 1000, state["th_mrad"] / 1000


class Motion:
    def __init__(self, link, safety, get, clock=time.perf_counter):
        self.link, self.safety, self.get, self.clock = link, safety, get, clock
        self._task: asyncio.Task | None = None
        self.busy = ""                       # what is running now, for state.json

    # ---- public API -------------------------------------------------------------
    async def move(self, meters: float, speed: float | None = None) -> dict:
        return await self._exclusive("move", self._move(meters, speed))

    async def turn(self, degrees: float, speed: float | None = None) -> dict:
        return await self._exclusive("turn", self._turn(math.radians(degrees), speed))

    async def recover(self, reason: str) -> dict:
        """After a firmware estop: bump/cliff -> clear the latch and back off
        safety.bumper_reverse_mm; tilt stays latched until a person says `clear`."""
        return await self._exclusive("recover", self._recover(reason))

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._send_stop()

    # ---- plumbing ---------------------------------------------------------------
    async def _exclusive(self, name, coro):
        self.stop()
        task = self._task = asyncio.ensure_future(coro)
        self.busy = name
        try:
            return await task
        except asyncio.CancelledError:
            if task.cancelled() and asyncio.current_task().cancelling() == 0:
                return {"ok": False, "reden": "onderbroken"}
            raise
        finally:
            if self._task is task:          # a newer request owns the motors now
                self.busy = ""
                self._send_stop()

    def _send_stop(self):
        try:
            self.link.stop()
        except ConnectionError:
            pass

    def _state(self):
        state = self.link.state
        age = time.perf_counter() - self.link.state_t if state else 1e9
        return state, age

    async def _wait_state(self, timeout=0.5):
        end = self.clock() + timeout
        while self.clock() < end:
            state, age = self._state()
            if state and age < 0.25:
                return state
            await asyncio.sleep(0.01)
        return None

    def _limits(self, speed):
        vmax = min(self.get("drive.max_speed"), abs(speed) if speed else 1e9)
        accel = self.get("drive.accel")
        alpha = accel * 2 / (self.get("drive.track_width") / 1000)   # wheel accel -> rad/s²
        return vmax, accel, alpha

    def _drive(self, v, w) -> str:
        """Send one gated command; returns the refusal reason ("" = sent)."""
        state, age = self._state()
        verdict = self.safety.gate(v, w, state, age)
        if verdict.refused:
            self._send_stop()
            return verdict.reason
        self.link.drive(verdict.v, verdict.w)
        if v > 0 and verdict.v == 0 and verdict.reason:
            return verdict.reason                  # obstacle ahead: stop, don't creep into it
        return ""

    # ---- move -------------------------------------------------------------------
    async def _move(self, meters, speed):
        state = await self._wait_state()
        if state is None:
            return {"ok": False, "reden": "geen verbinding met de motorbesturing"}
        x0, y0, th0 = pose(state)
        vmax, accel, _ = self._limits(speed)
        sign = 1 if meters >= 0 else -1
        goal = abs(meters)
        v, dt = 0.0, 1 / HZ
        deadline = self.clock() + goal / max(vmax, CREEP_V) * 2 + 3
        done = 0.0
        while True:
            state, _ = self._state()
            x, y, th = pose(state)
            done = sign * ((x - x0) * math.cos(th0) + (y - y0) * math.sin(th0))
            left = goal - done
            if left * 1000 <= MOVE_TOL_MM:
                return {"ok": True, "afstand_mm": round(done * 1000 * sign)}
            if self.clock() > deadline:
                return {"ok": False, "reden": "duurde te lang (vast?)", "afstand_mm": round(done * 1000 * sign)}
            want = min(vmax, math.sqrt(2 * accel * left), v + accel * dt)
            v = max(CREEP_V, want)
            w = max(-1.0, min(1.0, HEADING_GAIN * wrap(th0 - th)))
            refused = self._drive(sign * v, w)
            if refused:
                return {"ok": False, "reden": refused, "afstand_mm": round(done * 1000 * sign)}
            await asyncio.sleep(dt)

    # ---- turn -------------------------------------------------------------------
    async def _turn(self, radians, speed):
        state = await self._wait_state()
        if state is None:
            return {"ok": False, "reden": "geen verbinding met de motorbesturing"}
        _, _, th0 = pose(state)
        _, _, alpha = self._limits(None)
        wmax = min(abs(speed) if speed else 1.5, 1.5)
        turned_total, last_th, w, dt = 0.0, th0, 0.0, 1 / HZ
        deadline = self.clock() + abs(radians) / wmax * 2 + 3
        while True:
            state, _ = self._state()
            _, _, th = pose(state)
            turned_total += wrap(th - last_th)             # unwrapped: turns > 180° work
            last_th = th
            left = radians - turned_total
            if abs(left) <= TURN_TOL_RAD:
                return {"ok": True, "gedraaid_graden": round(math.degrees(turned_total))}
            if self.clock() > deadline:
                return {"ok": False, "reden": "duurde te lang (vast?)",
                        "gedraaid_graden": round(math.degrees(turned_total))}
            w = max(CREEP_W, min(wmax, math.sqrt(2 * alpha * abs(left)), abs(w) + alpha * dt))
            refused = self._drive(0.0, math.copysign(w, left))
            if refused:
                return {"ok": False, "reden": refused, "gedraaid_graden": round(math.degrees(turned_total))}
            await asyncio.sleep(dt)

    # ---- estop recovery ---------------------------------------------------------
    async def _recover(self, reason):
        if reason == "tilt":
            return {"ok": False, "reden": "gekanteld: blijft stilstaan tot iemand hem rechtzet"}
        self.link.send("clear")
        await asyncio.sleep(0.1)                           # let a fresh `st` show the latch gone
        back = self.get("safety.bumper_reverse_mm") / 1000
        if back <= 0:
            return {"ok": True, "reden": reason}
        result = await self._move(-back, RECOVER_V)
        result["reden"] = f"{reason}, {round(back * 1000)} mm teruggereden"
        return result
