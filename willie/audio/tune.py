"""The maths behind `make mic-tune` (tools/bench/mic_tune.py): judge the two INMP441 mics
and fit the numbers that sound direction and the wake word depend on.

Pure functions on 48 kHz stereo recordings (mic.command() format), so they are unit-tested
without a Pi. The interactive part (prompts, arecord, writing the settings) is the tool.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from willie.audio import doa, mic

TARGET_PEAK = 0.40              # speech peaks at this fraction of full scale after the wake gain
GAIN_RANGE = (1.0, 32.0)        # voice.wake_gain limits (config/schema.yaml)
WIDE_REACH = 0.20 / doa.SPEED_OF_SOUND * doa.CARD_RATE    # lag search for fitting: spacing up to 200 mm
OFFSET_RANGE = 10.0             # voice.doa_offset limits, samples
SPACING_RANGE = (0.03, 0.20)    # voice.doa_spacing limits, m

NOISE_OK, NOISE_WARN = 0.0025, 0.006        # rms, fraction of full scale
SNR_OK, SNR_WARN = 20.0, 12.0               # dB, speech over noise floor
BALANCE_OK, BALANCE_WARN = 3.0, 6.0         # dB difference between the channels
DC_WARN = 0.02
CLIP_LEVEL, CLIP_WARN = 0.98, 0.001
STALL_WARN = 1.05                           # recording took 5 % longer than its length = dropouts

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x - x.mean())))) if len(x) else 0.0


def _db(ratio: float) -> float:
    return 20 * float(np.log10(max(ratio, 1e-9)))


def _grade(value: float, ok: float, warn: float) -> str:
    """Lower is better."""
    return PASS if value <= ok else WARN if value <= warn else FAIL


def check_silence(raw: bytes, seconds: float, elapsed: float) -> list[Check]:
    """Noise floor, DC offset and card dropouts from a recording of a quiet room."""
    left, right = doa.split(raw)
    out = []
    for name, channel in (("left", left), ("right", right)):
        rms = _rms(channel)
        out.append(Check(f"noise floor {name}", _grade(rms, NOISE_OK, NOISE_WARN), f"{rms * 100:.2f} % FS rms"))
    dc = max(abs(float(left.mean())), abs(float(right.mean())))
    out.append(Check("DC offset", PASS if dc <= DC_WARN else WARN, f"{dc * 100:.2f} % FS"))
    stall = elapsed / seconds if seconds else 1.0
    out.append(Check("card dropouts", PASS if stall <= STALL_WARN else FAIL,
                     f"recording took {stall:.2f}x its length" + ("" if stall <= STALL_WARN else " (arecord overruns)")))
    return out


def check_speech(raw: bytes, noise_rms: tuple[float, float]) -> list[Check]:
    """Level, clipping, left/right balance and signal-to-noise from someone talking about 1 m away."""
    left, right = doa.split(raw)
    out = []
    levels = (_rms(left), _rms(right))
    for name, level, floor in zip(("left", "right"), levels, noise_rms):
        snr = _db(level / max(floor, 1e-6))
        status = PASS if snr >= SNR_OK else WARN if snr >= SNR_WARN else FAIL
        out.append(Check(f"speech over noise {name}", status,
                         f"{snr:.0f} dB (speech {level * 100:.2f} % FS rms; direction needs > {doa.QUIET * 100:.1f} %)"))
    balance = abs(_db(levels[0] / max(levels[1], 1e-9)))
    out.append(Check("left/right balance", _grade(balance, BALANCE_OK, BALANCE_WARN),
                     f"{balance:.1f} dB apart (mic holes blocked or one mic weaker?)"))
    clipped = float(np.mean((np.abs(left) > CLIP_LEVEL) | (np.abs(right) > CLIP_LEVEL)))
    out.append(Check("clipping at the card", PASS if clipped <= CLIP_WARN else FAIL, f"{clipped * 100:.3f} % of samples"))
    return out


def gain_for(raw: bytes) -> tuple[float, float]:
    """(voice.wake_gain, speech peak as fraction of FS at gain 1) so a normal voice peaks at
    TARGET_PEAK after the gain, measured the way the wake word hears it: left channel,
    decimated to 16 kHz. The 99.9th percentile ignores single clicks."""
    samples = mic.Stream(1.0).decimate(raw)
    peak = float(np.percentile(np.abs(samples), 99.9)) / 32768
    if peak <= 0:
        return GAIN_RANGE[1], 0.0
    return float(np.clip(TARGET_PEAK / peak, *GAIN_RANGE)), peak


def window_lag(raw: bytes) -> tuple[float, int] | None:
    """(median lag in samples, clear windows) of one standing position, with a search wide
    enough for any spacing; None when fewer than doa.MIN_CLEAR windows are clear."""
    left, right = doa.split(raw)
    lags = []
    for i in range(0, len(left) - doa.WINDOW + 1, doa.HOP):
        found = doa.measure(left[i:i + doa.WINDOW], right[i:i + doa.WINDOW], reach=WIDE_REACH)
        if found and found[1] >= doa.CLEAR:
            lags.append(found[0])
    if len(lags) < doa.MIN_CLEAR:
        return None
    return float(np.median(lags)), len(lags)


def fit(points: list[tuple[float, float]]) -> tuple[doa.Calibration, float] | None:
    """Fit lag = k * sin(angle) + offset to [(true angle, lag in samples)], angle + = towards
    the right channel's mic. Returns (calibration, rms error in degrees), or None with fewer
    than two different angles or a slope too small to mean anything."""
    if len({round(a) for a, _ in points}) < 2:
        return None
    sines = np.sin(np.radians([a for a, _ in points]))
    lags = np.array([lag for _, lag in points])
    slope, offset = np.polyfit(sines, lags, 1)
    if abs(slope) < 1.0:
        return None
    flip = bool(slope < 0)
    spacing = abs(slope) * doa.SPEED_OF_SOUND / doa.CARD_RATE
    cal = doa.Calibration(float(np.clip(spacing, *SPACING_RANGE)), float(np.clip(offset, -OFFSET_RANGE, OFFSET_RANGE)), flip)
    errors = []
    for angle, lag in points:
        sine = np.clip((lag - cal.offset) / doa.CARD_RATE * doa.SPEED_OF_SOUND / cal.spacing, -1, 1)
        heard = float(np.degrees(np.arcsin(sine)))
        errors.append((-heard if flip else heard) - angle)
    return cal, float(np.sqrt(np.mean(np.square(errors))))


def check_fit(cal: doa.Calibration, rms_error: float) -> list[Check]:
    out = [Check("direction fit error", PASS if rms_error <= 10 else WARN if rms_error <= 20 else FAIL,
                 f"{rms_error:.1f} deg rms over the positions you stood at")]
    out.append(Check("effective spacing", PASS if 0.06 <= cal.spacing <= 0.13 else WARN,
                     f"{cal.spacing * 1000:.0f} mm (hole distance in the lid is the real limit)"))
    out.append(Check("zero-angle lag", PASS if abs(cal.offset) <= 3 else WARN,
                     f"{cal.offset:+.2f} samples ({cal.offset / doa.CARD_RATE * 1e6:+.0f} us)"))
    return out


def score(trials: list[tuple[float, float | None]]) -> tuple[int, int, list[Check]]:
    """[(true angle, heard angle or None)] -> (within 30 deg, tries, checks). G4 wants 8 of 10."""
    within = sum(heard is not None and abs(heard - true) <= 30 for true, heard in trials)
    status = PASS if within >= 0.8 * len(trials) else WARN if within >= 0.6 * len(trials) else FAIL
    return within, len(trials), [Check("turns within 30 deg", status, f"{within} of {len(trials)} (G4 needs 8 of 10)")]


def gain_note(new: float, old: float) -> Check:
    """The wake model was trained at the old gain: say so when the change is big."""
    jump = max(new, old) / max(min(new, old), 1e-6)
    return Check("wake gain change", PASS if jump <= 1.5 else WARN,
                 f"{old:.1f} -> {new:.1f}" + ("" if jump <= 1.5 else " (big change: re-run make wake-test afterwards)"))
