#!/usr/bin/env python3
"""Guided microphone tuning: `make mic-tune` (about 3 minutes, on the Pi, voice service paused).

  1. quiet room        noise floor, DC offset, card dropouts
  2. you talk          level, clipping, left/right balance, signal-to-noise, wake gain
  3. 3 positions       0 / +90 / -90 degrees: fits sign, effective spacing and zero-angle lag
  4. 10 random tries   how many turns land within 30 degrees (G4 wants 8 of 10)
  5. motors running    optional: noise floor while he drives (you move him from the app)
Then it shows a PASS/WARN/FAIL table and asks before writing voice.wake_gain, voice.doa_spacing,
voice.doa_offset and voice.doa_flip. The old values go to .local/mic_tune_backup.json first;
`make mic-tune-undo` puts them back.

Positions: 1 m from the robot, talk steadily ("one two three ...") the whole time. + = the side
of the RIGHT channel's mic, seen from behind the robot looking forward.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from willie.audio import doa, mic, tune  # noqa: E402
from willie.config import Config  # noqa: E402

BACKUP = REPO / ".local" / "mic_tune_backup.json"
KEYS = {"voice": ["wake_gain", "doa_spacing", "doa_offset", "doa_flip"]}
FIT_ANGLES = (0.0, 90.0, -90.0)
TRY_ANGLES = (-90.0, -60.0, -30.0, 0.0, 30.0, 60.0, 90.0)
TRIES = 10
BYTES_PER_SECOND = doa.CARD_RATE * 8


def read(process, seconds: float) -> tuple[bytes, float]:
    start = time.monotonic()
    raw = process.stdout.read(int(seconds * doa.CARD_RATE) * 8)
    return raw, time.monotonic() - start


def drain(process) -> None:
    """Drop what piled up in the pipe while the person read the prompt."""
    process.stdout.read(int(0.4 * BYTES_PER_SECOND))


def ask(prompt: str) -> None:
    input(f"\n{prompt}\n  press Enter to start... ")
    print("  (recording)")


def current(config: Config) -> dict:
    return {sec: {key: config.get(f"{sec}.{key}") for key in keys} for sec, keys in KEYS.items()}


def undo() -> None:
    if not BACKUP.exists():
        sys.exit("No backup found: nothing to undo.")
    Config().update(json.loads(BACKUP.read_text()))
    print(f"Old values restored from {BACKUP}:\n{BACKUP.read_text()}")


def stand(process, angle: float) -> float | None:
    """Record one standing position, return its median lag (None = no clear voice, asks to retry)."""
    while True:
        ask(f"Stand at {angle:+.0f} deg, ~1 m away, and keep talking for 4 s.")
        drain(process)
        raw, _ = read(process, 4.0)
        found = tune.window_lag(raw)
        if found:
            print(f"  lag {found[0]:+.2f} samples ({found[1]} clear windows)")
            return found[0]
        if input("  no clear voice (too quiet or too noisy). Try again? [Y/n] ").strip().lower() == "n":
            return None


def main() -> None:
    if "undo" in sys.argv:
        return undo()
    config = Config()
    old = current(config)
    checks: list[tune.Check] = []
    process = subprocess.Popen(mic.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(0.8)                               # card settling
    new_gain, cal = None, None
    try:
        # 1. quiet room
        ask("Step 1/5: be quiet. Keep the room as it normally is, but nobody talking, for 10 s.")
        drain(process)
        raw, elapsed = read(process, 10.0)
        checks += tune.check_silence(raw, 10.0, elapsed)
        left, right = doa.split(raw)
        floor = (tune._rms(left), tune._rms(right))

        # 2. level, gain
        ask("Step 2/5: talk normally, 1 m in front of him, for 6 s (\"one two three ...\").")
        drain(process)
        raw, _ = read(process, 6.0)
        checks += tune.check_speech(raw, floor)
        new_gain, peak = tune.gain_for(raw)
        checks.append(tune.Check("speech peak at gain 1", tune.PASS if peak >= 0.01 else tune.WARN,
                                 f"{peak * 100:.1f} % FS; gain {new_gain:.1f} makes it {tune.TARGET_PEAK * 100:.0f} %"))
        checks.append(tune.gain_note(new_gain, old["voice"]["wake_gain"]))

        # 3. direction fit
        print("\nStep 3/5: direction. Three positions: ahead, 90 deg to the right-mic side, 90 deg to the other.")
        points = []
        for angle in FIT_ANGLES:
            lag = stand(process, angle)
            if lag is not None:
                points.append((angle, lag))
        fitted = tune.fit(points)
        if fitted is None:
            checks.append(tune.Check("direction fit", tune.FAIL, "not enough clear positions to fit"))
        else:
            cal, rms_error = fitted
            checks += tune.check_fit(cal, rms_error)

            # 4. verify with the fitted values
            print(f"\nStep 4/5: {TRIES} random positions to check the fit. I name each angle, you go there and talk.")
            angles = [random.choice(TRY_ANGLES) for _ in range(TRIES)]
            trials = []
            for number, target in enumerate(angles, 1):
                ask(f"Try {number}/{TRIES}: stand at {target:+.0f} deg and talk for 3 s.")
                drain(process)
                raw, _ = read(process, 3.0)
                result = doa.bearing(raw, cal)
                heard = result[0] if result else None
                trials.append((target, heard))
                print("  no clear voice" if heard is None else f"  heard {heard:+.0f} deg, error {heard - target:+.0f}")
            checks += tune.score(trials)[2]

        # 5. motors (optional)
        if input("\nStep 5/5 (optional): measure noise while he drives? [y/N] ").strip().lower() == "y":
            ask("Drive him around or move the servos from the app for 10 s, then stay quiet yourself.")
            drain(process)
            raw, _ = read(process, 10.0)
            left, right = doa.split(raw)
            over = tune._db(max(tune._rms(left), tune._rms(right)) / max(max(floor), 1e-6))
            checks.append(tune.Check("noise with motors running", tune.PASS if over <= 6 else tune.WARN if over <= 12 else tune.FAIL,
                                     f"{over:+.0f} dB over the quiet floor (direction while driving needs a clear voice above this)"))
    except KeyboardInterrupt:
        print("\nStopped, nothing written.")
        return
    finally:
        process.terminate()

    print("\n=== Result ===")
    for check in checks:
        print(f"  {check.status:4}  {check.name:28} {check.detail}")
    changes = {"voice": {"wake_gain": round(new_gain, 1)}} if new_gain else {}
    if cal:
        changes.setdefault("voice", {}).update(doa_spacing=round(cal.spacing, 4), doa_offset=round(cal.offset, 2), doa_flip=cal.flip)
    if not changes:
        return print("\nNothing to write.")
    print("\nNew values:", json.dumps(changes["voice"]), "\nOld values:", json.dumps(old["voice"]))
    if input("Write the new values? [y/N] ").strip().lower() != "y":
        return print("Not written.")
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(old, indent=2))
    config.update(changes)
    print(f"Written. Old values saved in {BACKUP} (make mic-tune-undo restores them).")


if __name__ == "__main__":
    main()
