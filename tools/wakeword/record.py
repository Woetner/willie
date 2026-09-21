#!/usr/bin/env python3
"""Record Wouter saying "Hey Willie" through the robot's own mic, for training round 2 (D2).

Run on the Pi, in an interactive terminal:
    ssh -t willie.local "cd willie && .venv/bin/python tools/wakeword/record.py"

1. 60 wake phrases, 2 s each: "Hey Willie" and "Willie" on its own (both wake him, 21 Sep),
   in blocks with a different instruction each (normal, soft, from 2 m, fast, slow) -
   variation is what makes the model generalise.
2. 2 min of normal talk (read aloud, chat) and 1 min of room sound with TV/radio:
   everyday sound that must NOT wake him.

Files land in ~/willie/.local/wakeword_rec/{positive,negative}/. Fetch them with
`make wake-fetch` and retrain (tools/wakeword/README.md, round 2).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
OUT = Path(__file__).resolve().parents[2] / ".local" / "wakeword_rec"
BLOCKS = (
    ("Zeg 'Hey Willie', normaal, zoals je hem straks roept, op ~1 m", 10),
    ("Zeg alleen 'Willie', normaal, op ~1 m", 10),
    ("Zeg 'Hey Willie' zachter, alsof hij naast je staat", 5),
    ("Zeg alleen 'Willie', zachter", 5),
    ("Zeg 'Hey Willie' vanaf ~2 m of vanuit de andere kant van de kamer", 5),
    ("Zeg alleen 'Willie' vanaf ~2 m", 5),
    ("Zeg 'Hey Willie' of 'Willie' snel en achteloos, wissel af", 10),
    ("Zeg 'Willie?' vragend, of met iets ervoor ('oké Willie', 'hé Willie')", 10),
)


def record(path: Path, seconds: float) -> float:
    subprocess.run(["arecord", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", "16000", "-c", "1",
                    "-d", str(int(seconds)) if seconds >= 1 else "1", str(path)], check=True)
    import array
    import wave
    with wave.open(str(path)) as w:
        samples = array.array("h", w.readframes(w.getnframes()))
    return max(map(abs, samples), default=0) / 327.68


def main() -> int:
    (OUT / "positive").mkdir(parents=True, exist_ok=True)
    (OUT / "negative").mkdir(parents=True, exist_ok=True)
    start = len(list((OUT / "positive").glob("*.wav")))
    print(f"Opnemen in {OUT} (al {start} opnames). Ctrl-C stopt; wat er is blijft bewaard.\n")
    n = start
    for instruction, count in BLOCKS:
        input(f"--- {instruction}. Enter om te beginnen ---")
        for i in range(count):
            print(f"  [{i + 1}/{count}]  zeg het NU ... ", end="", flush=True)
            peak = record(OUT / "positive" / f"wouter_{n:03d}.wav", 2)
            n += 1
            print(f"ok ({peak:.0f} % FS){'  - te zacht? ' if peak < 5 else ''}")
            time.sleep(0.6)
    print(f"\n{n - start} wake-opnames gemaakt.\n")

    input("--- 2 min gewoon praten: lees iets voor of vertel wat je vandaag doet, "
          "zonder 'Willie' te zeggen - woorden als 'wil je', 'Willem', 'willen' zijn juist goed. "
          "Enter om te beginnen ---")
    stamp = time.strftime("%H%M%S")
    record(OUT / "negative" / f"talk_{stamp}.wav", 120)
    input("--- 1 min kamergeluid: tv of radio aan, jij stil. Enter om te beginnen ---")
    record(OUT / "negative" / f"room_{stamp}.wav", 60)
    print("\nKlaar. Op de Mac: make wake-fetch")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ngestopt - opnames tot nu toe zijn bewaard")
        sys.exit(0)
