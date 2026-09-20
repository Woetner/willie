"""Microphone capture for WILL-E: one INMP441 on the I2S bus (B7).

The mic sits on the left channel (L/R tied to GND) of a 48 kHz S32_LE stereo
stream. Speech goes to the cloud as 16 kHz mono 16-bit WAV, which is what the
speech models want and is a ninth of the bytes.

Stdlib only, on purpose: `audioop` is gone in Python 3.13 and numpy is not in
the Pi's budget (D20/D21), so levels are measured with array min/max, which
runs in C.
"""

from __future__ import annotations

import array
import io
import subprocess
import wave

DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
RATE = 48_000
# The cloud is sent the mic's native 48 kHz. Decimating to 16 kHz first was
# tried on 20 Sep and made the speech unrecognisable to the model: averaging
# every three samples with no anti-alias filter smears the consonants.
OUT_RATE = RATE
CHANNELS = 2               # the I2S frame is stereo; only the left half is the mic
CHUNK_FRAMES = 2400        # 50 ms

FULL_SCALE = 2 ** 31
# Measured on the bench: quiet room peaks around 1 % FS, speech at 30 cm is
# 20-40 %. Halfway between, in the log domain, separates them cleanly.
SPEECH_PEAK = 0.04
SILENCE_PEAK = 0.015


def _peak(samples: array.array) -> float:
    if not samples:
        return 0.0
    return max(max(samples), -min(samples)) / FULL_SCALE


def _left_channel(raw: bytes) -> array.array:
    frame_bytes = 4 * CHANNELS
    samples = array.array("i")
    samples.frombytes(raw[: len(raw) // frame_bytes * frame_bytes])
    return samples[0::CHANNELS]


def to_wav(left: array.array) -> bytes:
    """32-bit 48 kHz mono -> 16-bit 48 kHz mono WAV. Width only, no resampling."""
    out = array.array("h", (max(-32768, min(32767, v >> 16)) for v in left))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(out.tobytes())
    return buffer.getvalue()


def listen(
    max_seconds: float = 15.0,
    silence_seconds: float = 1.2,
    lead_in_seconds: float = 6.0,
    on_level=None,
) -> tuple[bytes, float]:
    """Record until the speaker stops. Returns (wav_bytes, seconds_of_audio).

    Recording ends after `silence_seconds` of quiet once speech has started, or
    at `max_seconds`. If nobody ever speaks it gives up after `lead_in_seconds`
    so the console does not hang on a dead mic.
    """
    process = subprocess.Popen(
        ["arecord", "-D", DEVICE, "-f", "S32_LE", "-r", str(RATE), "-c", str(CHANNELS), "-t", "raw", "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    collected = array.array("i")
    chunk_seconds = CHUNK_FRAMES / RATE
    elapsed = quiet = 0.0
    speaking = False
    try:
        while elapsed < max_seconds:
            raw = process.stdout.read(CHUNK_FRAMES * 4 * CHANNELS) if process.stdout else b""
            if not raw:
                break
            left = _left_channel(raw)
            collected.extend(left)
            elapsed += chunk_seconds
            peak = _peak(left)
            if on_level:
                on_level(peak)
            if peak >= SPEECH_PEAK:
                speaking, quiet = True, 0.0
            elif speaking and peak < SILENCE_PEAK:
                quiet += chunk_seconds
                if quiet >= silence_seconds:
                    break
            elif not speaking and elapsed >= lead_in_seconds:
                break
    finally:
        process.terminate()
        process.wait(timeout=2)
    return (to_wav(collected) if speaking else b""), len(collected) / RATE
