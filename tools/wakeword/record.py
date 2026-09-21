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


def record(path: Path, seconds: float, meter: bool = False, lead: float = 0.0, cue: str = "") -> float:
    """Record `seconds` of 16 kHz mono to `path`; returns the peak in % of full scale.
    With `meter`, prints a live timer and level bar once a second (long recordings)."""
    import array
    import wave
    proc = subprocess.Popen(["arecord", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", "16000", "-c", "1",
                             "-t", "raw"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # The mic needs ~0.8 s to settle after arecord opens (a decaying bump that looks like
    # sound): record `lead` seconds first, throw them away, and only then show the cue.
    if lead:
        proc.stdout.read(int(16000 * lead) * 2)
    if cue:
        print(cue, end="", flush=True)
    total, peak = int(16000 * seconds), 0.0
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(16000)
        done = 0
        try:
            while done < total:
                chunk = proc.stdout.read(min(16000, total - done) * 2)
                if not chunk:
                    break
                out.writeframes(chunk)
                done += len(chunk) // 2
                second = max(map(abs, array.array("h", chunk)), default=0) / 327.68
                peak = max(peak, second)
                if meter:
                    bar = "#" * min(40, int(second))
                    print(f"\r  {done // 16000:3d}/{int(seconds)} s  {second:5.1f} % {bar:<40}", end="", flush=True)
        finally:
            proc.terminate()
            proc.wait()
    if meter:
        print()
    return peak


def main() -> int:
    (OUT / "positive").mkdir(parents=True, exist_ok=True)
    (OUT / "negative").mkdir(parents=True, exist_ok=True)
    start = len(list((OUT / "positive").glob("*.wav")))
    print(f"Opnemen in {OUT} (al {start} opnames). Ctrl-C stopt; wat er is blijft bewaard.\n")
    input("--- Microfooncheck: praat 5 s gewoon, vanaf waar je hem straks roept. Enter ---")
    peak = record(Path("/tmp/willie_miccheck.wav"), 5, meter=True, lead=0.8)
    if peak < 10:
        print(f"  Je stem komt maar op {peak:.0f} % binnen (nodig: 10 % of meer). Staat het gaatje van de"
              f" INMP441 naar je toe en ligt het niet tegen de tafel? Los dat eerst op.")
        if input("  Toch doorgaan? (j/N) ").strip().lower() != "j":
            return 1
    else:
        print(f"  Goed: {peak:.0f} %.\n")
    n = start
    for instruction, count in BLOCKS:
        input(f"--- {instruction}. Enter om te beginnen ---")
        for i in range(count):
            peak = record(OUT / "positive" / f"wouter_{n:03d}.wav", 2, lead=0.8,
                          cue=f"  [{i + 1}/{count}]  zeg het NU ... ")
            n += 1
            print(f"ok ({peak:.0f} % FS){'  - te zacht, kom dichterbij' if peak < 10 else ''}")
            time.sleep(0.6)
    print(f"\n{n - start} wake-opnames gemaakt.\n")

    input("--- 2 min gewoon praten: lees iets voor of vertel wat je vandaag doet, "
          "zonder 'Willie' te zeggen - woorden als 'wil je', 'Willem', 'willen' zijn juist goed. "
          "Enter om te beginnen ---")
    stamp = time.strftime("%H%M%S")
    record(OUT / "negative" / f"talk_{stamp}.wav", 120, meter=True)
    input("--- 1 min kamergeluid: tv of radio aan, jij stil. Enter om te beginnen ---")
    record(OUT / "negative" / f"room_{stamp}.wav", 60, meter=True)
    print("\nKlaar. Op de Mac: make wake-fetch")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ngestopt - opnames tot nu toe zijn bewaard")
        sys.exit(0)
