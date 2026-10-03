"""Deep sleep (Phase P, D31): low power while asleep, and falling asleep by himself.

Asleep = `privacy.mute`, the one switch the app, the dashboard and the touch screen share. This
module runs in the core and follows that switch:
  - the ESP32 gets `sleep 1|0` (sensors off, 80 MHz, `st` at sleep.mcu_hz; drive and look refused),
  - the Pi goes through the root helper `willie-power-mode sleep|wake` (tools/power_mode.sh:
    CPU governor, Wi-Fi power save, ACT LED, Spotify).
The face (black screen, backlight, wake on a tap) and the voice process (no wake word) follow the
same switch on their own.

He falls asleep by himself (sleep.auto):
  - after sleep.idle_min with no conversation, command, touch or mission,
  - always at night: at behavior.quiet_start as soon as nothing is running, and when woken during
    the night again after sleep.night_idle_min. At quiet_end he wakes up, if the night put him to sleep.
Garage mode, a conversation and a mission keep him awake. Waking is only done by the app, the
dashboard or a tap (remote.py, dashboard, face/runtime.py).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import subprocess
import threading
import time
from datetime import datetime

from willie.behavior.tree import in_quiet_hours

log = logging.getLogger("willie.sleep")

HELPER = "/usr/local/sbin/willie-power-mode"
_jobs: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None


def power_mode(mode: str) -> None:
    """Pi side of sleep/wake, on one worker thread: in order, and the core's loop never waits
    (stopping Spotify can take a second). Without the helper (Mac, not installed yet) only the MCU sleeps."""
    global _worker
    _jobs.put(mode)
    if _worker is None:
        _worker = threading.Thread(target=_work, name="power-mode", daemon=True)
        _worker.start()


def _work() -> None:
    while True:
        _run_helper(_jobs.get())


def _run_helper(mode: str) -> None:
    try:
        out = subprocess.run(["sudo", "-n", HELPER, mode], capture_output=True, text=True, timeout=15)
        if out.returncode:
            log.warning("power mode %s failed: %s", mode, (out.stderr or out.stdout).strip()[:200])
        else:
            log.info("pi %s", out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("power mode %s not available: %s", mode, exc)


class Sleep:
    def __init__(self, cfg, link, body, clock=time.monotonic, now=datetime.now, run=power_mode):
        self.cfg, self.link, self.body = cfg, link, body
        self.clock, self.now, self.run = clock, now, run
        self._applied = None            # last state sent to the MCU + Pi (None = not yet)
        night = self._night()
        self._was_night = night
        self._night_slept = night and self.asleep()   # restarted at night while asleep: wake him in the morning
        self._night_due = False

    def asleep(self) -> bool:
        return bool(self.cfg.get("privacy.mute"))

    def _night(self) -> bool:
        return in_quiet_hours(self.now(), str(self.cfg.get("behavior.quiet_start")),
                              str(self.cfg.get("behavior.quiet_end")))

    def _busy(self) -> str:
        body = self.body
        if body.conversation:
            return "conversation"
        if body.mission:
            return "mission"
        if body.motion.busy:
            return "moving"
        if self.cfg.get("modes.garage"):
            return "garage"
        return ""

    def set(self, asleep: bool, why: str) -> None:
        if self.asleep() != asleep:
            log.info("%s (%s)", "falling asleep" if asleep else "waking up", why)
            self.cfg.update({"privacy": {"mute": asleep}})
        self.apply()

    def apply(self) -> None:
        """Bring the MCU and the Pi in line with the switch (called on every change and every tick)."""
        want = self.asleep()
        if want == self._applied:
            return
        if self.link.stats.connected:
            self.link.sleep(want)
        self.run("sleep" if want else "wake")
        self._applied = want
        if not want:
            self.body.last_activity = self.clock()   # just woken: a full idle time before sleeping again
            self._night_due = False
        log.info("deep sleep %s", "on" if want else "off")

    def on_hello(self) -> None:
        """The ESP32 restarted: it always boots awake."""
        if self.asleep():
            self.link.sleep(True)

    def tick(self) -> None:
        self.apply()
        night = self._night()
        if night != self._was_night:
            self._was_night = night
            if night:
                self._night_due = not self.asleep()
                self._night_slept = False
            elif self._night_slept and self.asleep() and self.cfg.get("sleep.auto"):
                self._night_slept = False
                self.set(False, "morning")
                return
        if self.asleep() or not self.cfg.get("sleep.auto") or self._busy():
            return
        if night and self._night_due:
            self._night_slept = True
            self.set(True, "night")
            return
        limit = self.cfg.get("sleep.night_idle_min" if night else "sleep.idle_min") * 60
        if self.clock() - self.body.last_activity >= limit:
            self._night_slept = night
            self.set(True, f"nothing happened for {limit / 60:.0f} min")

    async def run_loop(self, every_s: float = 2.0):
        while True:
            self.tick()
            await asyncio.sleep(every_s)
