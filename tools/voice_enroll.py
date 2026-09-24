#!/usr/bin/env python3
"""Teach the home server Wouter's voice for garage mode (K1, S15): `make voice-enroll`.

Records through the robot's own mic (same path and gain as the live session), in rounds of
ROUND_S seconds, and sends each round to the server's voice check (stem_leer). Do it where
garage mode will run - in the garage, at working distance - so the profile matches.

    make voice-enroll            add to the profile
    make voice-enroll NEW=1      start the profile over
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from ask_camera import load_env                    # noqa: E402
from willie.audio import mic                       # noqa: E402

ROUNDS, ROUND_S = 3, 20
TEXTS = (
    "Ik sta in de garage en werk aan de Kever. Willie, wat is het aanhaalmoment van de cilinderkop, "
    "en in welke volgorde moeten de moeren? Zet ook even een timer van tien minuten voor de lijm.",
    "Welke pin van de ESP32 is SDA, en mag ik daar vijf volt op zetten? Laat de pinout van de "
    "TB6612 zien. Zet een BC547 en een setje krimpkous op de boodschappenlijst.",
    "Praat gewoon zoals je in de werkplaats praat: vragen, opmerkingen, een beetje mopperen. "
    "Hoe zit dat met de ontsteking van de Tomos? Klopt deze bedrading? Wat is dit voor onderdeel?",
)


def record(seconds: float) -> bytes:
    process = subprocess.Popen(mic.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        process.stdout.read(int(mic.RATE * 0.8) * mic.FRAME)       # the mic settling
        raw = process.stdout.read(int(mic.RATE * seconds) * mic.FRAME)
    finally:
        process.terminate()
        process.wait()
    return mic.left(raw, mic.gain())


def main() -> int:
    load_env(REPO / ".env")
    from willie.remote import Remote
    remote = Remote.from_env()
    if remote is None:
        print("MQTT_HOST missing from .env: the home server is needed for this", file=sys.stderr)
        return 2
    for _ in range(50):
        if remote.client.is_connected():
            break
        time.sleep(0.1)
    fresh = os.environ.get("NEW") == "1"
    name = os.environ.get("NAME", "wouter")
    print(f"Voice profile '{name}'{' (starting over)' if fresh else ''}: {ROUNDS} rounds of {ROUND_S} s.")
    print("Talk at working distance from the robot, at your normal volume. Only you, no radio.\n")
    try:
        for i in range(ROUNDS):
            print(f"Round {i + 1}/{ROUNDS} - read this aloud (or just talk):\n\n  {TEXTS[i % len(TEXTS)]}\n")
            input("Press Enter and start talking... ")
            pcm = record(ROUND_S)
            print("  sending...", flush=True)
            result = remote.hub_call("stem_leer", {"pcm": base64.b64encode(pcm).decode("ascii"), "naam": name,
                                                   "vervang": fresh and i == 0}, 60)
            if "fout" in result or "detail" in result:
                print(f"  failed: {result}")
                return 1
            print(f"  ok: {result.get('toegevoegd')} pieces, profile {result.get('samples')} pieces, "
                  f"threshold {result.get('threshold')}, self-match {result.get('self_scores')}\n")
    finally:
        remote.close()
    print("Done. Garage mode now answers only this voice (settings: garage.voice_check).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
