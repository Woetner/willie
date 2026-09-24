"""Mood engine (G2, WILL-E.md §3.2): five values from 0 to 1 that jump on events and
drift back to a baseline. Tiny and pure: no threads, no I/O except reading the weights.

- energy, curiosity, happiness drift to their baselines (mood.*_base) at mood.drift_rate;
  energy's baseline also follows the clock (fresh in the morning, low late in the evening)
- boredom rises towards 1 while nothing happens (mood.boredom_rate); events push it down
- attention fades to 0 (mood.attention_decay) after a wake word, a face or a touch

Outputs: `snapshot()` for the dashboard graphs, `words()` / `context()` for the AI's live
context line ("stemming: nieuwsgierig, een beetje moe; batterij 64 %"), and `face()` for
the resting expression.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from pathlib import Path

import yaml

log = logging.getLogger("willie.mood")

NAMES = ("energy", "curiosity", "happiness", "boredom", "attention")
WEIGHTS_FILE = Path(__file__).resolve().parents[2] / "config" / "mood.yaml"


def load_weights(path: Path = WEIGHTS_FILE) -> dict[str, dict[str, float]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    weights = {}
    for event, deltas in raw.items():
        good = {k: float(v) for k, v in (deltas or {}).items() if k in NAMES}
        if len(good) != len(deltas or {}):
            log.warning("mood.yaml %s: unknown mood name ignored (%s)", event, set(deltas) - set(NAMES))
        weights[event] = good
    return weights


def energy_clock(hour: float) -> float:
    """Offset on the energy baseline by time of day: +0.15 at 9:00, -0.25 at 23:00-5:00."""
    if hour < 5 or hour >= 23:
        return -0.25
    return 0.15 * math.cos((hour - 9) / 14 * math.pi) - 0.05 * (hour > 19) * (hour - 19)


class Mood:
    def __init__(self, get, weights=None, clock=time.monotonic, now=datetime.now):
        self.get, self.clock, self.now = get, clock, now
        self.weights = load_weights() if weights is None else weights
        self.values = {n: self._base(n) for n in NAMES}
        self.values["boredom"] = 0.2
        self._t = clock()

    def _base(self, name: str) -> float:
        if name == "energy":
            t = self.now()
            return min(1, max(0, self.get("mood.energy_base") + energy_clock(t.hour + t.minute / 60)))
        if name in ("curiosity", "happiness"):
            return self.get(f"mood.{name}_base")
        return 0.0

    def update(self) -> dict:
        """Advance the drift to now. Cheap: call it at 1 Hz or before every read."""
        t = self.clock()
        minutes, self._t = (t - self._t) / 60, t
        if minutes <= 0:
            return self.values
        drift = 1 - math.exp(-self.get("mood.drift_rate") * minutes)
        for name in ("energy", "curiosity", "happiness"):
            self.values[name] += (self._base(name) - self.values[name]) * drift
        self.values["boredom"] += (1 - self.values["boredom"]) * (1 - math.exp(-self.get("mood.boredom_rate") * minutes))
        self.values["attention"] *= math.exp(-self.get("mood.attention_decay") * minutes)
        return self.values

    def event(self, name: str) -> bool:
        """Apply one event's weights. Returns False for an event without weights."""
        deltas = self.weights.get(name)
        if not deltas:
            return False
        self.update()
        for mood, delta in deltas.items():
            self.values[mood] = min(1.0, max(0.0, self.values[mood] + delta))
        return True

    def snapshot(self) -> dict:
        self.update()
        return {n: round(v, 3) for n, v in self.values.items()}

    # ---- outputs ------------------------------------------------------------
    def words(self) -> str:
        """Two or three Dutch words for the AI, strongest feelings first."""
        v = self.snapshot()
        parts = []
        if v["attention"] > 0.6:
            parts.append("alert")
        if v["curiosity"] > 0.75:
            parts.append("nieuwsgierig")
        if v["happiness"] > 0.75:
            parts.append("vrolijk")
        elif v["happiness"] < 0.3:
            parts.append("wat mistroostig")
        if v["boredom"] > 0.7:
            parts.append("verveeld")
        if v["energy"] < 0.2:
            parts.append("erg moe")
        elif v["energy"] < 0.4:
            parts.append("een beetje moe")
        return ", ".join(parts[:3]) or "rustig"

    def context(self, battery: int | None = None, place: str = "") -> str:
        line = f"stemming: {self.words()}"
        if battery is not None:
            line += f"; batterij {battery} %"
        if place:
            line += f"; kamer: {place}"
        return line

    def face(self) -> str:
        """Resting expression (face/renderer.py STATES) when nothing else drives the face."""
        v = self.snapshot()
        if v["energy"] < 0.15:
            return "sleep"
        if v["happiness"] < 0.25:
            return "sad"
        if v["curiosity"] > 0.8 or v["attention"] > 0.6:
            return "curious"
        if v["happiness"] > 0.85:
            return "happy"
        return "idle"
