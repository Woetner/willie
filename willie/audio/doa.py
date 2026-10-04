"""Which way is the voice? Direction of arrival from the two INMP441 mics (G4).

The mics sit on one I2S bus, left and right channel, `SPACING` apart. A voice reaches the
nearer mic a little earlier: at most SPACING / c = ~290 us for 100 mm, only ~14 samples at
the card's 48 kHz (which is why this runs on the raw 48 kHz stereo, not the 16 kHz mono
every other reader gets: at 16 kHz one sample is already ~12 degrees).

The delay comes from GCC-PHAT (same idea as clean.lag), refined to a fraction of a sample
by parabolic interpolation. Two mics cannot tell front from back: the angle is measured
from straight ahead and a voice behind gives the same value as one in front. The caller
turns towards it and lets the camera decide.

Angle: 0 = straight ahead, + = the voice is nearer the RIGHT channel's mic, - = nearer the
left channel's. Which physical side that is depends on the wiring, and the spacing and a
constant lag also differ from the ideal, so `make mic-tune` (audio/tune.py) measures
spacing, offset and flip and stores them as voice.doa_*.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CARD_RATE = 48_000
SPEED_OF_SOUND = 343.0
SPACING = 0.078                 # m, EFFECTIVE: fitted on the bench 3 Oct (sin of the heard angle was
                                # 0.78x the true one at 0/±45/±90 with 0.10). Replace by the measured
                                # mic-hole distance if that differs a lot.
BAND = (300.0, 4000.0)          # voice; below is rumble and above the phase wraps at 100 mm
QUIET = 0.004                   # rms (fraction of full scale) below which nothing is a voice
CLEAR = 4.0                     # correlation peak / median needed to trust one window


@dataclass(frozen=True)
class Calibration:
    """What `make mic-tune` measured: effective mic spacing (m), the lag in samples a voice
    straight ahead already shows (wiring/clock skew), and whether the sign is mirrored."""
    spacing: float = SPACING
    offset: float = 0.0
    flip: bool = False


def calibration() -> Calibration:
    """The live settings (voice.doa_*); the defaults when there is no settings file yet."""
    try:
        from willie.config import LIVE_PATH, Config
        if not LIVE_PATH.exists():
            return Calibration()
        config = Config()
        return Calibration(float(config.get("voice.doa_spacing")), float(config.get("voice.doa_offset")),
                           bool(config.get("voice.doa_flip")))
    except Exception:
        return Calibration()


def measure(left: np.ndarray, right: np.ndarray, rate: int = CARD_RATE, reach: float | None = None,
            ) -> tuple[float, float] | None:
    """(lag in samples, clarity) of `left` against `right` by GCC-PHAT, or None when too
    quiet. Lag > 0 = `left` is later = the voice is nearer the right mic. `reach` is how many
    samples either way to look (default: what SPACING allows). Clarity is the correlation
    peak over the median: ~1 for noise, 10+ for one clear voice."""
    left = np.asarray(left, np.float32)
    right = np.asarray(right, np.float32)
    count = min(len(left), len(right))
    if count < rate // 20:
        return None
    left, right = left[:count], right[:count]
    left = left - left.mean()
    right = right - right.mean()
    if np.sqrt(np.mean(left ** 2)) < QUIET or np.sqrt(np.mean(right ** 2)) < QUIET:
        return None
    size = 1 << (2 * count - 1).bit_length()
    cross = np.fft.rfft(left, size) * np.conj(np.fft.rfft(right, size))
    band = np.fft.rfftfreq(size, 1 / rate)
    cross = np.where((band >= BAND[0]) & (band <= BAND[1]), cross / (np.abs(cross) + 1e-9), 0)
    corr = np.fft.irfft(cross, size)
    if reach is None:
        reach = SPACING / SPEED_OF_SOUND * rate
    span = int(reach) + 2
    lags = np.arange(-span, span + 1)
    values = corr[lags]                                       # negative lags wrap: fine for irfft
    best = int(np.argmax(values))
    clarity = float(values[best] / (np.median(np.abs(corr[: size // 2])) + 1e-9))
    offset = 0.0
    if 0 < best < len(values) - 1:                            # parabolic peak, sub-sample
        a, b, c = values[best - 1], values[best], values[best + 1]
        denom = a - 2 * b + c
        if denom:
            offset = 0.5 * (a - c) / denom
    return float(lags[best] + offset), clarity


def estimate(left: np.ndarray, right: np.ndarray, rate: int = CARD_RATE, spacing: float = SPACING,
             flip: bool = False, offset: float = 0.0) -> tuple[float, float] | None:
    """(angle in degrees, clarity) for one window of both channels, or None when it is
    too quiet to hear a voice."""
    found = measure(left, right, rate, spacing / SPEED_OF_SOUND * rate + abs(offset))
    if found is None:
        return None
    lag, clarity = found
    # corr peaks at lag > 0 when `left` is later than `right`, i.e. the voice is nearer right
    sine = float(np.clip((lag - offset) / rate * SPEED_OF_SOUND / spacing, -1.0, 1.0))
    angle = float(np.degrees(np.arcsin(sine)))
    return (-angle if flip else angle), clarity


def combine(windows: list[tuple[float, float]]) -> tuple[float, int] | None:
    """Several windows -> (median angle, windows that were clear). One window is easily
    fooled by a wall reflection or a click; the median of ~6 half-second windows is not."""
    good = [angle for angle, clarity in windows if clarity >= CLEAR]
    if not good:
        return None
    return float(np.median(good)), len(good)


def split(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    """arecord's 48 kHz stereo S32_LE (mic.command()) -> left, right as floats in -1..1."""
    raw = raw[: len(raw) // 8 * 8]
    both = np.frombuffer(raw, "<i4").astype(np.float32) / 2 ** 31
    return both[0::2], both[1::2]


WINDOW = CARD_RATE // 2         # 0.5 s per estimate
HOP = WINDOW // 2
MIN_CLEAR = 2                   # clear windows needed before the robot moves
DEADBAND = 10.0                 # degrees: closer than this he is already facing you
MAX_TURN = 90.0                 # front hemisphere only: two mics cannot tell front from back


def bearing(raw: bytes, cal: Calibration | None = None) -> tuple[float, int] | None:
    """Angle of the voice in a stereo recording (mic.command() format), median over the
    overlapping half-second windows; None when fewer than MIN_CLEAR of them are clear."""
    cal = cal or calibration()
    left, right = split(raw)
    found = [estimate(left[i:i + WINDOW], right[i:i + WINDOW], spacing=cal.spacing, flip=cal.flip,
                      offset=cal.offset) for i in range(0, len(left) - WINDOW + 1, HOP)]
    result = combine([f for f in found if f])
    return result if result and result[1] >= MIN_CLEAR else None


def turn_for(raw: bytes, cal: Calibration | None = None) -> float | None:
    """How far the robot should turn to face the voice in `raw`, in motion.turn() degrees
    (+ = left, counter-clockwise; the voice angle + is to the right, so the sign flips).
    None = stay: no clear voice, or already within DEADBAND of facing it."""
    result = bearing(raw, cal)
    if result is None:
        return None
    angle = max(-MAX_TURN, min(MAX_TURN, result[0]))
    return None if abs(angle) < DEADBAND else -angle
