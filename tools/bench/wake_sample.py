#!/usr/bin/env python3
"""Record how Wouter actually says the wake word, so it can be tuned offline.

Run on the Pi:  .venv/bin/python tools/bench/wake_sample.py
Say the wake word three times, with a pause between them.
Saves /tmp/wake_sample.wav.
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
OUT = Path("/tmp/wake_sample.wav")
SECONDS = 10

print(f"Say the wake word 3 times, with a pause between. Recording {SECONDS} s...", flush=True)
raw = subprocess.run(
    ["arecord", "-D", DEVICE, "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(SECONDS), "-t", "raw", "-q"],
    capture_output=True).stdout
if not raw:
    sys.exit("nothing captured")
with wave.open(str(OUT), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(raw)
import array
s = array.array("h"); s.frombytes(raw[: len(raw)//2*2])
print(f"saved {OUT} ({len(s)/16000:.1f} s, peak {max(max(s), -min(s))/32768*100:.0f}% FS)")
