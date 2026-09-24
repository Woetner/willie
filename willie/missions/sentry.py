"""Sentry mode (K5): "bewaak het huis" - on/off by hand only (Wouter, 24 Sep).

Triggers, all checked locally or on the home server (D17: nothing goes to the cloud):
  sound   a sharp spike on his own mic, well above the room (the wake-word loop hands
          every chunk to `on_audio`, so "Hey Willie" keeps working meanwhile)
  bump    a jolt on the MPU6050 (someone knocks him or picks him up)
  camera  every `sentry.look_s` a photo to the server (`wacht_kijk`): a clear change
          against the last one, or a person, is an alarm - daytime only
  eufy    a person at an outdoor camera (the hub keeps the events, `wacht_status`)

On a trigger he turns towards it and looks round (G4 sound direction will let him drive
to the source), sends the photos to the phone (`wacht_alarm`: push + app) and stays on
guard. At night (quiet hours, or a dark frame) only sound, bump and eufy count. He does
not drive around on guard: the battery lasts longer next to the dock.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from collections import deque
from datetime import datetime

from . import Mission

log = logging.getLogger("willie.missions.sentry")

COOLDOWN_S = 60.0
SOUND_OVER_ROOM = 6.0
SOUND_MIN = 0.35               # of full scale, after the mic gain
BUMP_MG = 250
EUFY_POLL_S = 5.0
SCAN_PANS = (-70, 0, 70)

# The wake-word loop calls this with every mic chunk while a sentry runs (willie/voice/wake.py).
_active: "Sentry | None" = None


def on_audio(chunk: bytes) -> None:
    sentry = _active
    if sentry is not None:
        sentry.hear(chunk)


def _setting(key: str, default):
    try:
        from willie.config import Config
        return Config().get(key)
    except Exception:
        return default


def _peak(chunk: bytes) -> float:
    import array
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    return max(max(samples), -min(samples)) / 32768 if samples else 0.0


class Sentry(Mission):
    kind, label = "wacht", "SENTRY"

    def __init__(self, robot=None, **args):
        super().__init__(robot, **args)
        self.levels: deque[float] = deque(maxlen=100)       # ~10 s of chunk peaks
        self.triggers: deque[str] = deque(maxlen=10)
        self._lock = threading.Lock()
        self.last_alarm = 0.0
        self.alarms = 0

    # -- inputs -------------------------------------------------------------------
    def hear(self, chunk: bytes) -> None:
        peak = _peak(chunk)
        with self._lock:
            room = sorted(self.levels)[len(self.levels) // 5] if len(self.levels) >= 20 else None
            self.levels.append(peak)
            if room is not None and peak >= max(SOUND_MIN, room * SOUND_OVER_ROOM):
                self.triggers.append(f"geluid ({peak * 100:.0f}% van max)")

    def _bumped(self, last: list | None) -> tuple[bool, list | None]:
        imu = (self.state().get("imu") or {}).get("acc_mg")
        if not imu:
            return False, last
        if last is None:
            return False, imu
        jolt = max(abs(a - b) for a, b in zip(imu, last))
        return jolt > BUMP_MG, imu

    @staticmethod
    def _night() -> bool:
        from willie.behavior.tree import in_quiet_hours
        try:
            return in_quiet_hours(datetime.now(), _setting("behavior.quiet_start", "23:00"),
                                  _setting("behavior.quiet_end", "07:30"))
        except (TypeError, ValueError):
            return False

    # -- main loop ----------------------------------------------------------------
    def run(self) -> dict:
        global _active
        _active = self
        try:
            from willie.voice import wake
            wake.LEVEL_HOOK = on_audio          # the wake-word loop's mic chunks come here too
        except ImportError:
            wake = None
        self.show("sentry", "ON GUARD")
        look_every = float(_setting("sentry.look_s", 20))
        next_look = time.monotonic() + 3
        next_eufy = time.monotonic()
        imu = None
        dark = False
        try:
            while not self.stopped:
                reason = None
                with self._lock:
                    if self.triggers:
                        reason = self.triggers.popleft()
                        self.triggers.clear()
                bumped, imu = self._bumped(imu)
                if bumped and not reason:
                    reason = "iemand stootte tegen me aan"
                now = time.monotonic()
                if not reason and now >= next_eufy:
                    next_eufy = now + EUFY_POLL_S
                    events = self.robot.hub("wacht_status", {}, 5).get("events") or []
                    if events:
                        reason = f"persoon bij de {events[-1].get('camera', 'buitencamera')}"
                if not reason and now >= next_look and not (self._night() or dark):
                    next_look = now + look_every
                    jpeg = self.robot.photo()
                    if jpeg:
                        seen = self.robot.hub("wacht_kijk", {"jpeg": base64.b64encode(jpeg).decode("ascii")}, 20)
                        dark = bool(seen.get("donker"))
                        if seen.get("alarm"):
                            reason = seen.get("reden") or "er veranderde iets"
                elif dark and now >= next_look:
                    next_look = now + look_every * 6          # check now and then whether the lights are on
                    dark = False
                if reason and now - self.last_alarm >= COOLDOWN_S:
                    self._alarm(reason)
                self.progress = f"op wacht, {self.alarms} meldingen"
                if self.wait(0.5):
                    break
        finally:
            _active = None
            if wake is not None:
                wake.LEVEL_HOOK = None
        return {"meldingen": self.alarms, "laatste": list(self.triggers)}

    def _alarm(self, reason: str) -> None:
        """Turn towards it, look round, photos to the phone."""
        self.last_alarm = time.monotonic()
        self.alarms += 1
        log.warning("sentry alarm: %s", reason)
        self.show("surprised", "WAT WAS DAT?")
        photos = []
        for pan in SCAN_PANS:
            self.look(pan, 0, settle=0.5)
            jpeg = self.robot.photo()
            if jpeg:
                photos.append(base64.b64encode(jpeg).decode("ascii"))
        self.look(0, settle=0.1)
        self.robot.hub("wacht_alarm", {"reden": reason, "fotos": photos,
                                       "nacht": self._night()}, 30)
        self.show("sentry", "ON GUARD")
