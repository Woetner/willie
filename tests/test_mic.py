"""willie.audio.mic: left channel, 48 -> 16 kHz decimation, gain, clipping (the INMP441 is on the left)."""
import numpy as np

from willie.audio import mic


def _stereo(left: np.ndarray) -> bytes:
    frames = np.zeros((len(left), 2), "<i4")
    frames[:, 0] = left
    frames[:, 1] = 999 << 16                     # the empty right channel must be ignored
    return frames.tobytes()


def test_level_matches_the_old_16_bit_capture():
    tone = (np.sin(2 * np.pi * 1000 * np.arange(4800) / mic.CARD_RATE) * (1000 << 16)).astype(np.int64)
    out = np.frombuffer(mic.left(_stereo(tone), 8.0), "<i2")
    assert len(out) == 1600                      # 3:1
    assert 7600 < np.abs(out[200:]).max() < 8400  # 1000 (old S16) x 8, passband flat


def test_alias_is_filtered_and_clipping():
    hiss = (np.sin(2 * np.pi * 12_000 * np.arange(4800) / mic.CARD_RATE) * (1000 << 16)).astype(np.int64)
    out = np.frombuffer(mic.left(_stereo(hiss), 8.0), "<i2")
    assert np.abs(out[200:]).max() < 20          # 12 kHz would fold onto 4 kHz without the FIR
    loud = np.full(300, 20_000 << 16, np.int64)
    assert np.frombuffer(mic.left(_stereo(loud), 8.0), "<i2")[-1] == 32767


def test_chunks_join_without_a_seam():
    tone = (np.sin(2 * np.pi * 440 * np.arange(9600) / mic.CARD_RATE) * (3000 << 16)).astype(np.int64)
    raw = _stereo(tone)
    whole = np.frombuffer(mic.Stream(2.0).convert(raw), "<i2")
    stream = mic.Stream(2.0)
    parts = np.frombuffer(b"".join(stream.convert(raw[i:i + 1000 * 8]) for i in range(0, len(raw), 1000 * 8)), "<i2")
    assert np.array_equal(whole, parts)          # odd chunk sizes keep the 3:1 phase
    assert "-c" in mic.command() and mic.command()[mic.command().index("-c") + 1] == "2"
    assert mic.FRAME == 24                       # raw bytes per 16 kHz sample
