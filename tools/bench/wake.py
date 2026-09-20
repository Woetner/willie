#!/usr/bin/env python3
"""B7/D2 bench test: does "hey gemini" fire, and how often does it fire wrongly?

Run on the Pi:  .venv/bin/python tools/bench/wake.py [seconds]

Prints every detection with its timestamp, and anything the recogniser thought
it nearly heard. No API calls, so this costs nothing to run for an hour with
the TV on - which is exactly the D2 false-trigger test.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from willie.voice import wake


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    if not wake.available():
        print(f"No Vosk model at {wake.MODEL_DIR} - see config/wakewords/README.md", file=sys.stderr)
        return 2
    print(f"listening for {wake.describe()} for {seconds:.0f} s - say it whenever you like")
    started = time.monotonic()
    hits = 0
    while time.monotonic() - started < seconds:
        remaining = seconds - (time.monotonic() - started)
        heard = wake.listen_for_wake(
            stop_after=remaining,
            on_tick=lambda _, partial: print(f"    ...heard: {partial!r}", flush=True),
        )
        if heard:
            hits += 1
            print(f"  [{time.monotonic() - started:5.1f}s] DETECTED  (#{hits})", flush=True)
    print(f"\n  {hits} detection(s) in {seconds:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
