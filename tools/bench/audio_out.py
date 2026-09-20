#!/usr/bin/env python3
"""B6 bench test: MAX98357A I2S amp + speaker.

Done when: a WAV plays cleanly at a comfortable volume with no hiss when silent.
Run on the Pi:  .venv/bin/python tools/bench/audio_out.py   (or `make bench-audio-out`)

Wouter listens and answers; the script only sets up the signals and checks the
mechanical side (card present, formats accepted, no ALSA underruns).
"""

from __future__ import annotations

import array
import math
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

RATE = 48_000
CARD_HINT = "MAX98357A"


def card() -> str | None:
    out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if CARD_HINT.lower() in line.lower() and line.startswith("card "):
            number = line.split()[1].rstrip(":")
            device = line.split("device ")[1].split(":")[0]
            return f"hw:{number},{device}"
    return None


def write_wav(path: Path, samples: array.array, channels: int = 2) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


def tone(seconds: float, freq: float, amplitude: float, fade: float = 0.01) -> array.array:
    """Stereo 16-bit sine with short fades, so the amp does not get a step edge."""
    n = int(RATE * seconds)
    peak = amplitude * 32767
    fade_n = max(1, int(RATE * fade))
    data = array.array("h")
    for i in range(n):
        gain = min(1.0, i / fade_n, (n - i) / fade_n)
        value = int(peak * gain * math.sin(2 * math.pi * freq * i / RATE))
        data.append(value)
        data.append(value)
    return data


def sweep(seconds: float, f0: float, f1: float, amplitude: float) -> array.array:
    n = int(RATE * seconds)
    peak = amplitude * 32767
    fade_n = max(1, int(RATE * 0.01))
    data = array.array("h")
    phase = 0.0
    for i in range(n):
        freq = f0 * (f1 / f0) ** (i / n)  # log sweep: sounds even across the range
        phase += 2 * math.pi * freq / RATE
        gain = min(1.0, i / fade_n, (n - i) / fade_n)
        value = int(peak * gain * math.sin(phase))
        data.append(value)
        data.append(value)
    return data


def silence(seconds: float) -> array.array:
    return array.array("h", [0] * (2 * int(RATE * seconds)))


def play(device: str, path: Path, fmt: str = "S16_LE") -> tuple[bool, str]:
    proc = subprocess.run(
        ["aplay", "-D", device, "-f", fmt, "-r", str(RATE), "-c", "2", str(path)],
        capture_output=True, text=True, check=False,
    )
    noise = (proc.stderr or "").lower()
    underrun = "underrun" in noise or "overrun" in noise
    return proc.returncode == 0 and not underrun, (proc.stderr or "").strip()


def ask(question: str) -> bool:
    try:
        return input(f"    {question} [y/N] ").strip().lower().startswith("y")
    except EOFError:
        print("    (no keyboard - answer skipped)")
        return False


def main() -> int:
    device = card()
    if not device:
        sys.exit("FAIL no MAX98357A card in `aplay -l` - is dtoverlay=max98357a in config.txt?")
    print(f"== B6 audio out: {device} @ {RATE} Hz ==")

    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        clips = [
            ("1 kHz tone, -12 dBFS, 3 s", tone(3.0, 1000, 0.25), "a steady clean tone, no crackle"),
            ("log sweep 100 Hz - 8 kHz, 4 s", sweep(4.0, 100, 8000, 0.25), "smooth from low to high, no rattle or dropout"),
            ("speech-band chord, 3 s", None, "full and undistorted at a comfortable volume"),
        ]
        chord = array.array("h")
        for base in (220.0,):
            parts = [tone(3.0, f, 0.12) for f in (base, base * 1.5, base * 2.0)]
            for i in range(len(parts[0])):
                chord.append(max(-32767, min(32767, sum(p[i] for p in parts))))
        clips[2] = (clips[2][0], chord, clips[2][2])

        for name, samples, question in clips:
            path = d / "clip.wav"
            write_wav(path, samples)
            print(f"-- {name}")
            ok, err = play(device, path)
            if not ok:
                print(f"   aplay problem: {err.splitlines()[-1] if err else 'non-zero exit'}")
            results.append((name, ok and ask(question + "?")))

        print("-- digital silence, 5 s (listen close to the cone)")
        path = d / "silence.wav"
        write_wav(path, silence(5.0))
        ok, err = play(device, path)
        results.append(("silent = quiet", ok and ask("silent, no hiss or hum?")))

        print("-- format check: S32_LE")
        path = d / "clip32.wav"
        write_wav(path, tone(0.5, 1000, 0.25))
        ok32, _ = play(device, path, fmt="S32_LE")
        print(f"   S32_LE {'accepted' if ok32 else 'refused (S16_LE is enough for B6)'}")

    print()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(ok for _, ok in results)
    print(f"\n  {'PASS' if passed else 'FAIL'}  B6 audio out")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
