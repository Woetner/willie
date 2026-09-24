"""Short notification tones on WILL-E's own speaker (22 Sep, Wouter: a little sound when his
mode changes). Generated here, no sound files: two soft notes, rising for "awake",
falling for "asleep"."""
from __future__ import annotations

import array
import math

RATE = 48_000                    # the card's own rate (B6), so no resampling
NOTE_S, GAP_S, LEVEL = 0.13, 0.03, 0.6   # before voice.volume; a sine needs more than speech
TONES = {
    "idle": (659.3, 987.8),      # E5 -> B5, rising: awake
    "sleep": (987.8, 587.3),     # B5 -> D5, falling: going to sleep
    "notify": (880.0, 1318.5),   # A5 -> E6, bright: something happened
}


def pcm(kind: str) -> bytes:
    out = array.array("h")
    for n, freq in enumerate(TONES.get(kind, TONES["notify"])):
        count = int(NOTE_S * RATE)
        for i in range(count):
            t = i / RATE
            # 5 ms attack, exponential decay: a soft "ding", no click.
            envelope = min(1.0, t / 0.005) * math.exp(-t * 18)
            out.append(int(32767 * LEVEL * envelope * math.sin(2 * math.pi * freq * t)))
        if n == 0:
            out.extend([0] * int(GAP_S * RATE))
    return out.tobytes()


def play(kind: str) -> None:
    """Blocking, ~0.3 s. Uses the same aplay path and volume as his voice."""
    from willie.audio import speech
    speech._play_pcm(pcm(kind), RATE)
