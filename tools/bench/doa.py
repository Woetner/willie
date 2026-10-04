#!/usr/bin/env python3
"""Sound direction bench (G4): which way is the voice, from the two INMP441 mics.

Run on the Pi with the voice service stopped:  make bench-doa
  make bench-doa A="0 45 -45 90 -90"   asks you to talk from each angle, reports the error
  make bench-doa FLIP=--flip           swap the sign (if + turns out to be your left)

Without angles it prints a live angle every half second until Ctrl-C.
Angle: 0 = straight ahead, + = towards the RIGHT mic, as seen from behind the robot
looking forward (check it: talk from the side where the right mic is, expect a positive angle).
Stand ~1 m away, keep talking steadily ("one two three...") for the 4 s of each position.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from willie.audio import doa, mic  # noqa: E402

WINDOW = doa.CARD_RATE // 2                       # 0.5 s per angle estimate
FLIP = "--flip" in sys.argv
ANGLES = [float(a) for a in sys.argv[1:] if not a.startswith("--")]


def read(process, seconds: float) -> tuple[np.ndarray, np.ndarray]:
    raw = process.stdout.read(int(seconds * doa.CARD_RATE) * 8)
    return doa.split(raw)


def windows(left: np.ndarray, right: np.ndarray) -> list[tuple[float, float]]:
    out = []
    for start in range(0, len(left) - WINDOW + 1, WINDOW):
        found = doa.estimate(left[start:start + WINDOW], right[start:start + WINDOW], flip=FLIP)
        if found:
            out.append(found)
    return out


def live(process) -> None:
    print("live angle, Ctrl-C to stop (-- = too quiet; * = not clear, ignored)")
    while True:
        left, right = read(process, 0.5)
        found = doa.estimate(left, right, flip=FLIP)
        if not found:
            print("  --")
        else:
            angle, clarity = found
            mark = " " if clarity >= doa.CLEAR else "*"
            print(f"{mark} {angle:+6.1f}°  clarity {clarity:5.1f}  rms L {np.sqrt(np.mean(left**2))*100:4.1f}% R {np.sqrt(np.mean(right**2))*100:4.1f}%")


def positions(process) -> None:
    errors = []
    for target in ANGLES:
        input(f"\nStand at {target:+.0f}° (0 = ahead, + = right mic side), then press Enter and talk for 4 s... ")
        read(process, 0.3)                        # drop what piled up in the pipe while waiting
        left, right = read(process, 4.0)
        result = doa.combine(windows(left, right))
        if result is None:
            print("  no clear voice (too quiet or too noisy)")
            errors.append(None)
            continue
        angle, count = result
        errors.append(angle - target)
        print(f"  heard {angle:+6.1f}°  error {angle - target:+6.1f}°  ({count} of 8 windows clear)")
    done = [e for e in errors if e is not None]
    if done:
        within = sum(abs(e) <= 30 for e in done)
        print(f"\nwithin ±30°: {within} of {len(ANGLES)}   mean |error| {np.mean(np.abs(done)):.1f}°")


def main() -> None:
    process = subprocess.Popen(mic.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(0.8)                               # card settling
    try:
        positions(process) if ANGLES else live(process)
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()


if __name__ == "__main__":
    main()
