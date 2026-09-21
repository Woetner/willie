"""The robot's microphone as the wake word and the recorder hear it (D2).

Two findings from 21 Sep, measured on the Pi:
- The INMP441 sits on the LEFT I2S channel; the right one is empty. Capturing mono made
  ALSA average the two, which halved every level.
- It is a quiet mic by design: a normal voice at 1 m peaks at a few % of full scale, while
  the noise floor is 0.1-0.2 % rms. So a fixed digital gain is applied (voice.wake_gain).

Wake-word training recordings (tools/wakeword/record.py) go through this same path, so
the model hears Wouter at training time exactly as it will when listening.
"""
from __future__ import annotations

import array
import sys

DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
RATE = 16_000
FRAME = 4                       # bytes per stereo S16 frame
DEFAULT_GAIN = 8.0


def command() -> list[str]:
    """arecord, 16 kHz stereo S16 raw to stdout (read FRAME bytes per sample)."""
    return ["arecord", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(RATE), "-c", "2", "-t", "raw"]


def gain() -> float:
    try:
        from willie.config import Config
        return float(Config().get("voice.wake_gain"))
    except Exception:
        return DEFAULT_GAIN


def left(raw: bytes, factor: float = DEFAULT_GAIN) -> bytes:
    """Stereo S16 -> the left channel, amplified and clipped, as mono S16."""
    raw = raw[: len(raw) // FRAME * FRAME]
    try:
        import numpy as np
        samples = np.frombuffer(raw, "<i2")[0::2].astype(np.float32) * factor
        return np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        stereo = array.array("h", raw)
        if sys.byteorder != "little":
            stereo.byteswap()
        out = array.array("h", (max(-32768, min(32767, int(v * factor))) for v in stereo[0::2]))
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()
