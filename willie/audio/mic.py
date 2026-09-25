"""The robot's microphone as the wake word, the recorder and the live session hear it (D2).

Findings, measured on the Pi:
- The INMP441 sits on the LEFT I2S channel; the right one is empty. Capturing mono made
  ALSA average the two, which halved every level (21 Sep).
- It is a quiet mic by design: a normal voice at 1 m peaks at a few % of full scale, while
  the noise floor is 0.1-0.2 % rms. So a fixed digital gain is applied (voice.wake_gain).
- The card only runs 48 kHz S32_LE and delivers 24 real bits (25 Sep). Asking `plughw`
  for 16 kHz made ALSA's plug resampler convert through 16 bits with linear
  interpolation and no anti-alias filter, so hiss above 8 kHz folded back onto the
  consonants. Now the card's own format is captured and decimated 3:1 here, through a
  proper low-pass FIR (numpy, ~1 % of a core).

Every reader gets 16 kHz mono S16 with the gain applied, the same level as before, so the
trained wake-word model hears the same loudness. Readers keep one `Stream` per arecord
process: the filter carries its history across chunks, so chunk edges do not click.

Wake-word training recordings (tools/wakeword/record.py) go through this same path, so
the model hears Wouter at training time exactly as it will when listening.
"""
from __future__ import annotations

import numpy as np

DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
CARD_RATE = 48_000
RATE = 16_000                   # what every reader gets
DECIMATE = CARD_RATE // RATE
FRAME = 8 * DECIMATE            # raw bytes per 16 kHz output sample (stereo S32 at 48 kHz)
DEFAULT_GAIN = 8.0

# Windowed-sinc low-pass at 48 kHz: flat to ~6 kHz, -60 dB from ~8 kHz (the 16 kHz
# Nyquist), 95 taps = 1 ms of delay.
_TAPS = 95
_CUTOFF = 7_000 / CARD_RATE
_n = np.arange(_TAPS) - (_TAPS - 1) / 2
FIR = (2 * _CUTOFF * np.sinc(2 * _CUTOFF * _n) * np.blackman(_TAPS)).astype(np.float32)
FIR /= FIR.sum()


def command() -> list[str]:
    """arecord in the card's native format: 48 kHz stereo S32 raw to stdout."""
    return ["arecord", "-q", "-D", DEVICE, "-f", "S32_LE", "-r", str(CARD_RATE), "-c", "2", "-t", "raw"]


def gain() -> float:
    try:
        from willie.config import Config
        return float(Config().get("voice.wake_gain"))
    except Exception:
        return DEFAULT_GAIN


class Stream:
    """48 kHz stereo S32 -> 16 kHz mono S16 (left channel), low-passed, gained, clipped."""

    def __init__(self, factor: float = DEFAULT_GAIN) -> None:
        self.factor = factor
        self.history = np.zeros(_TAPS - 1, np.float32)
        self.phase = 0              # where the next 3:1 output sample sits in the next chunk

    def decimate(self, raw: bytes) -> np.ndarray:
        """16 kHz float samples at the old S16 level (S32 / 65536), no gain yet."""
        raw = raw[: len(raw) // 8 * 8]
        left = np.frombuffer(raw, "<i4")[0::2].astype(np.float32) / 65536
        signal = np.concatenate((self.history, left))
        filtered = np.convolve(signal, FIR, "valid")        # one output per new input sample
        self.history = signal[len(signal) - (_TAPS - 1):]
        picked = filtered[self.phase::DECIMATE]
        self.phase = (self.phase - len(left)) % DECIMATE
        return picked

    def convert(self, raw: bytes) -> bytes:
        return s16(self.decimate(raw), self.factor)


def s16(samples: np.ndarray, factor: float) -> bytes:
    return np.clip(samples * factor, -32768, 32767).astype("<i2").tobytes()


def left(raw: bytes, factor: float = DEFAULT_GAIN) -> bytes:
    """One-shot conversion of a whole recording (a fresh filter, fine for a single read)."""
    return Stream(factor).convert(raw)
