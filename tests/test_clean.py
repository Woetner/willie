"""willie.audio.clean: the echo reference timeline, and the speex cleaner where libspeexdsp exists."""
import numpy as np
import pytest

from willie.audio import clean


def test_reference_lines_up_played_audio_with_capture_time():
    ref = clean.EchoReference(delay=0.05)
    pcm = (np.arange(2400) % 100 + 1).astype("<i2").tobytes()            # 100 ms at 24 kHz
    ref.play(pcm, 24_000, at=10.0)
    before = ref.read(10.0, 800)                                         # still in the delay
    during = ref.read(10.05, 1600)
    after = ref.read(10.16, 800)
    assert not before.any() and during.all() and not after.any()
    ref.cut(10.0)                                                        # killed before it sounded
    assert not ref.read(10.05, 1600).any()


def test_reference_gap_is_silence():
    ref = clean.EchoReference(delay=0.0)
    ones = np.ones(2400, "<i2").tobytes()
    ref.play(ones, 24_000, at=1.0)
    ref.play(ones, 24_000, at=2.0)
    assert not ref.read(1.2, 1600).any() and ref.read(2.0, 1600).all()


def test_aligner_finds_the_real_delay():
    rng = np.random.default_rng(2)
    ref = clean.EchoReference(delay=0.10)                                # the guess
    noise = (rng.standard_normal(24_000 * 3) * 4000).astype("<i2")
    ref.play(noise.tobytes(), 24_000, at=0.0)
    aligner = clean.Aligner(ref)
    true = 0.25
    for n in range(20):                                                   # 2 s of 100 ms chunks
        at = 0.3 + n * 0.1
        played = ref.read(at, 1600)
        start = round((at - true) * 16_000)
        room = np.interp(np.arange(start, start + 1600) * 1.5, np.arange(len(noise)), noise.astype(np.float32))
        aligner.feed(room * 0.3, played)
    assert abs(ref.delay - true) < 0.002


@pytest.mark.skipif(clean.library() is None, reason="libspeexdsp not installed (Pi only)")
def test_echo_canceller_removes_a_played_signal():
    rng = np.random.default_rng(1)
    played = (rng.standard_normal(16_000 * 4) * 3000).astype(np.int16)
    heard = (np.convolve(played, [0, 0, 0.5, 0.2, -0.1])[: len(played)]).astype(np.int16)  # a room
    cleaner = clean.Cleaner.load(-15, echo=True)
    out = np.frombuffer(cleaner.process(heard.tobytes(), played), "<i2")
    cleaner.close()
    tail = slice(16_000 * 3, None)                                       # after it adapted
    assert np.abs(out[tail]).mean() < 0.1 * np.abs(heard[tail]).mean()
