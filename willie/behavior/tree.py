"""Behaviour selection (G3, WILL-E.md §3.4) - classic code, no AI (§1.2).

A priority list, checked at 2 Hz: the first behaviour that wants to run owns the body;
when the winner changes, the running one is cancelled (its motion stops with it).

    hold      estop latched, no MCU state, or battery empty  -> never move by himself
    talk      a conversation is open                          -> sit still, face the person
    respect   someone drove him in the last behavior.respect_s -> leave him where he was put
    quiet     quiet hours (behavior.quiet_start..quiet_end)   -> no wandering
    nap       mood energy < NAP_ENERGY                        -> rest (G6 adds: drive to the dock)
    wander    behavior.wander on                              -> pause, turn, drive, look around
    idle      otherwise

Still to come with the body/sensors: come say hi (G4 sound direction), follow (G5),
patrol (H7), dock (G6), curiosity poke on a loud sound (G4).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime

log = logging.getLogger("willie.behavior")

HZ = 2
NAP_ENERGY = 0.15
LOOK_AROUND = ((-40, 5), (40, 5), (0, 0))       # pan/tilt stops after a wander leg


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    minutes = now.hour * 60 + now.minute
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    return s <= minutes < e if s <= e else minutes >= s or minutes < e


class Behaviours:
    def __init__(self, body, get, clock=time.monotonic, now=datetime.now, rng=None, sleep=asyncio.sleep):
        self.body, self.get, self.clock, self.now = body, get, clock, now
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.current = "idle"
        self._task: asyncio.Task | None = None

    # ---- selection ----------------------------------------------------------
    def choose(self) -> str:
        body = self.body
        st = body.link.state
        age = time.perf_counter() - body.link.state_t if st else None
        if st is None or age is None or age > 0.5 or st.get("estop") or body.safety.battery_state == "cutoff":
            return "hold"
        if body.conversation:
            return "talk"
        if body.last_command and self.clock() - body.last_command < self.get("behavior.respect_s"):
            return "respect"
        if in_quiet_hours(self.now(), self.get("behavior.quiet_start"), self.get("behavior.quiet_end")):
            return "quiet"
        if body.mood.snapshot()["energy"] < NAP_ENERGY:
            return "nap"
        if self.get("behavior.wander"):
            return "wander"
        return "idle"

    async def run(self):
        try:
            while True:
                self.step()
                await asyncio.sleep(1 / HZ)
        finally:
            self._cancel()

    def step(self) -> str:
        pick = self.choose()
        if pick != self.current:
            log.info("behaviour %s -> %s", self.current, pick)
            self._cancel()
            self.current = pick
            if pick == "wander":
                self._task = asyncio.ensure_future(self.wander())
        return pick

    def _cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
            if self.body.motion.busy:            # stop only a motion that belongs to us
                self.body.motion.stop()
        self._task = None

    # ---- behaviours ---------------------------------------------------------------
    def pause_s(self) -> float:
        """Average wander pause, shorter when curious and awake (§3.4)."""
        mood = self.body.mood.snapshot()
        drive = 0.5 + mood["curiosity"] * mood["energy"] + 0.5 * mood["boredom"]   # ~0.5 .. 2
        return self.get("behavior.wander_pause_s") / drive * self.rng.uniform(0.5, 1.5)

    async def wander(self):
        """Pause, turn somewhere new, drive a short leg, stop and look around. Repeat."""
        while True:
            await self.sleep(self.pause_s())
            turned = await self.body.motion.turn(self.rng.choice((-1, 1)) * self.rng.uniform(30, 150))
            leg = self.rng.uniform(0.2, 1.0) * self.get("behavior.wander_max_m")
            moved = await self.body.motion.move(leg)
            self.body.mood.event("driving")
            if not moved.get("ok") and "obstakel" in str(moved.get("reden", "")):
                await self.body.motion.turn(self.rng.choice((-1, 1)) * 120)   # F6: turn away
            for pan, tilt in LOOK_AROUND:
                try:
                    self.body.link.look(pan, tilt)
                except ConnectionError:
                    break
                await self.sleep(1.2)
            if not turned.get("ok") and "onderbroken" in str(turned.get("reden", "")):
                return
