"""Missions (Phase K, D25): search (K3), adventure (K4) and sentry (K5).

A mission is a background thread in the voice process: that process owns the camera, the
face, the speaker and the MQTT link to the home server. It drives the body only through
the core's control socket (willie/control.py), so the safety gate (F1) and the firmware
watchdog stay in charge of every wheel turn. The heavy seeing happens on the home server
(S15, hub/missions.py); the robot sends JPEGs and gets names and places back.

One mission at a time. The core's behaviour tree sits still while one runs
(control event "mission_start"/"mission_end"), like it does for a conversation.

    start("zoek", naam="de kat")   start("avontuur")   start("wacht")   stop()   status()
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("willie.missions")

# Set by tools/willie_voice.py (like the skills): the hub call and the face.
HUB_CALL = None
FACE = None
BATTERY_HOME_V = 10.8          # ~30 % of a 3S pack under light load: stop and go home


class Robot:
    """What a mission may touch. Tests pass a fake with the same methods."""

    def body(self, cmd: str, timeout: float = 30.0, **args) -> dict:
        from willie import control
        return control.request(cmd, timeout=timeout, **args)

    def photo(self) -> bytes | None:
        """A quick still (~1 s): 1024x768 JPEG, autofocus on capture."""
        if not shutil.which("rpicam-still"):
            return None
        from willie.voice import tools
        frame = tools.FRAME_SOURCE() if tools.FRAME_SOURCE else None
        if frame:
            return frame
        with tempfile.TemporaryDirectory(prefix="willie-mission-") as tmp:
            path = Path(tmp) / "m.jpg"
            if FACE:
                FACE.indicators(camera=True)
            try:
                subprocess.run(["rpicam-still", "--nopreview", "--autofocus-on-capture", "--timeout", "700",
                                "--width", "1024", "--height", "768", "--quality", "85", "--output", str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            finally:
                if FACE:
                    FACE.indicators(camera=False)
            return path.read_bytes() if path.exists() and path.stat().st_size else None

    def hub(self, name: str, args: dict, timeout: float = 30.0) -> dict:
        if HUB_CALL is None:
            return {"fout": "de thuisserver is niet verbonden"}
        return HUB_CALL(name, args, timeout)

    def say(self, text: str) -> None:
        from willie.audio import speech
        speech.speak(text)

    @property
    def face(self):
        return FACE

    def sleep(self, seconds: float, stop: threading.Event) -> bool:
        """Sleep, but wake at once when the mission is stopped. True = stopped."""
        return stop.wait(seconds)


class Mission:
    kind = ""
    label = ""                 # face mode label
    started = 0.0

    def __init__(self, robot: Robot | None = None, **args):
        self.robot = robot or Robot()
        self.args = args
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.result: dict = {}
        self.progress = ""

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self.started = time.time()
        self.thread = threading.Thread(target=self._main, name=f"mission-{self.kind}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.robot.body("stop", timeout=2)

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _main(self) -> None:
        self.robot.body("event", timeout=2, name="mission_start")
        face = self.robot.face
        if face:
            face.mode(self.label)
        try:
            self.result = self.run() or {}
        except Exception as exc:
            log.exception("mission %s crashed", self.kind)
            self.result = {"fout": f"{type(exc).__name__}: {exc}"}
        finally:
            self.robot.body("stop", timeout=2)
            self.robot.body("look", timeout=2, pan=0, tilt=0)
            self.robot.body("event", timeout=2, name="mission_end")
            if face:
                face.mode("")
                face.set_state("idle", "SAY HEY WILLIE")
            log.info("mission %s ended: %s", self.kind, self.result)

    def run(self) -> dict:                   # pragma: no cover - subclasses
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def wait(self, seconds: float) -> bool:
        return self.robot.sleep(seconds, self.stop_event)

    def state(self) -> dict:
        return self.robot.body("state", timeout=2)

    def battery_low(self) -> bool:
        volts = self.state().get("battery_v")
        return bool(volts and volts > 5 and volts < BATTERY_HOME_V)

    def look(self, pan: float, tilt: float = 0.0, settle: float = 0.6) -> None:
        self.robot.body("look", timeout=2, pan=pan, tilt=tilt)
        self.wait(settle)

    def show(self, state: str, text: str = "") -> None:
        face = self.robot.face
        if face:
            try:
                face.set_state(state, text)
            except ValueError:                  # a face without this state (older renderer)
                face.set_state("seeing", text)


_current: Mission | None = None
_lock = threading.Lock()


def start(kind: str, robot: Robot | None = None, **args) -> dict:
    global _current
    from . import adventure, search, sentry
    classes = {"zoek": search.Search, "avontuur": adventure.Adventure, "wacht": sentry.Sentry}
    if kind not in classes:
        return {"fout": f"onbekende missie {kind}"}
    with _lock:
        if _current and _current.running:
            if _current.kind == kind:
                return {"fout": f"{kind} loopt al", "voortgang": _current.progress}
            _current.stop()
            _current.thread.join(5)
        _current = classes[kind](robot, **args)
        _current.start()
    return {"ok": True, "gestart": kind}


def stop() -> dict:
    with _lock:
        if not _current or not _current.running:
            return {"ok": True, "was": "niets"}
        _current.stop()
        kind = _current.kind
    _current.thread.join(10)
    return {"ok": True, "gestopt": kind, "resultaat": _current.result}


def status() -> dict:
    if not _current:
        return {"missie": None}
    return {"missie": _current.kind, "loopt": _current.running, "voortgang": _current.progress,
            "resultaat": _current.result, "sinds_s": round(time.time() - _current.started)}
