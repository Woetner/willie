"""Adventure mode (K4, the ultimate test): explore the house and map what is where.

At every stop he turns to four headings; per heading he reads the front ToF (how far the
way is free) and takes three photos (head left, middle, right). Each photo goes to the
server with the pose from odometry (`avontuur_zie`): the server recognises things, people
and the cat (K2 memory, so "last seen" is updated too), pins new objects on the map at
that pose and decides which room the stop is in - that gives the room graph.

Where next: the heading with the most free space whose target cell (0.5 m grid) he has
not visited yet; a dead end takes the most open heading anyway. He stops when the
battery gets low, after `adventure.max_stops` stops or `adventure.max_min` minutes, or
on "stop". Then the server writes the adventure story (`avontuur_einde`) and he tells it.

Honest limits (24 Sep): odometry drifts, so the map is a sketch; visual SLAM on the
server is still an experiment (K4 plan); docking needs G6.
"""
from __future__ import annotations

import base64
import logging
import math
import time
import uuid

from . import Mission

log = logging.getLogger("willie.missions.adventure")

HEADINGS = (0, 90, 90, 90)      # relative turns: scan 4 directions, ending where he started + 270
PANS = (-45, 0, 45)
CELL_M = 0.5
MAX_LEG_M = 1.0
MIN_FREE_MM = 350


def _setting(key: str, default):
    try:
        from willie.config import Config
        return Config().get(key)
    except Exception:
        return default


class Adventure(Mission):
    kind, label = "avontuur", "ADVENTURE"

    def run(self) -> dict:
        self.id = uuid.uuid4().hex[:8]
        max_stops = int(_setting("adventure.max_stops", 40))
        deadline = time.monotonic() + float(_setting("adventure.max_min", 30)) * 60
        visited: set[tuple[int, int]] = set()
        found = 0
        stops = 0
        reason = "klaar"
        self.robot.say("Ik ga op avontuur!")
        while not self.stopped:
            if stops >= max_stops:
                reason = f"{max_stops} plekken bekeken"
                break
            if time.monotonic() > deadline:
                reason = "tijd om"
                break
            if self.battery_low():
                reason = "batterij bijna leeg"
                break
            pose = self._pose()
            visited.add(self._cell(pose))
            options = []
            for turn in HEADINGS:
                if self.stopped:
                    break
                if turn:
                    self.robot.body("turn", deg=turn)
                pose = self._pose()
                free = self._free_mm()
                for pan in PANS:
                    self.look(pan, 0, settle=0.5)
                    jpeg = self.robot.photo()
                    if not jpeg:
                        continue
                    seen = self.robot.hub("avontuur_zie", {
                        "id": self.id, "jpeg": base64.b64encode(jpeg).decode("ascii"),
                        "pose": pose, "pan": pan, "vrij_mm": free, "stop": stops}, 45)
                    found += int(seen.get("nieuw") or 0)
                    self.show("seeing", f"ONTDEKT: {found}")
                options.append((pose["th_deg"], free))
            self.look(0, settle=0.2)
            stops += 1
            self.progress = f"{stops} plekken, {found} ontdekkingen"
            if self.stopped:
                break
            heading, free = self._choose(options, pose, visited)
            if free < MIN_FREE_MM:
                reason = "vastgelopen: nergens ruimte"
                break
            current = self._pose()["th_deg"]
            self.robot.body("turn", deg=_wrap(heading - current))
            moved = self.robot.body("move", m=min(MAX_LEG_M, (free - 250) / 1000))
            if not moved.get("ok") and "garagemodus" in str(moved.get("reden", "")):
                reason = "ik mag niet rijden (garagemodus)"
                break
        if self.stopped:
            reason = "gestopt"
        self.show("thinking", "MIJN AVONTUUR...")
        story = self.robot.hub("avontuur_einde", {"id": self.id, "reden": reason}, 90)
        if story.get("verhaal"):
            self.robot.say(story["verhaal"])
        return {"plekken": stops, "ontdekkingen": found, "reden": reason,
                "kamers": story.get("kamers"), "kaart": "in de app (Avontuur)"}

    # -- helpers ----------------------------------------------------------------
    def _pose(self) -> dict:
        pose = (self.state().get("pose") or {})
        return {"x_mm": pose.get("x_mm") or 0, "y_mm": pose.get("y_mm") or 0, "th_deg": pose.get("th_deg") or 0.0}

    def _free_mm(self) -> int:
        tof = self.state().get("tof_mm") or []
        centre = tof[1] if len(tof) > 1 else None
        return int(centre) if centre else 2000        # no reading = nothing within the ToF's range

    @staticmethod
    def _cell(pose: dict, heading: float | None = None, dist_mm: float = 0.0) -> tuple[int, int]:
        th = math.radians(pose["th_deg"] if heading is None else heading)
        x = pose["x_mm"] + dist_mm * math.cos(th)
        y = pose["y_mm"] + dist_mm * math.sin(th)
        return (round(x / 1000 / CELL_M), round(y / 1000 / CELL_M))

    def _choose(self, options: list[tuple[float, int]], pose: dict, visited: set) -> tuple[float, int]:
        fresh = [(h, f) for h, f in options
                 if f >= MIN_FREE_MM and self._cell(pose, h, min(MAX_LEG_M * 1000, f - 250)) not in visited]
        pool = fresh or options
        return max(pool, key=lambda o: o[1]) if pool else (pose["th_deg"], 0)


def _wrap(deg: float) -> float:
    return (deg + 180) % 360 - 180
