"""willie.audio.doa: the two-mic direction from a synthetic voice at a known angle."""
import numpy as np
import pytest

from willie.audio import doa

RATE = doa.CARD_RATE


@pytest.fixture(autouse=True)
def default_calibration(monkeypatch):
    monkeypatch.setattr(doa, "calibration", doa.Calibration)


def voice(seconds=0.5, seed=1):
    """Band-limited noise with a speech-like envelope, so the correlation has one peak."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freq = np.fft.rfftfreq(n, 1 / RATE)
    spectrum[(freq < 200) | (freq > 4000)] = 0
    wave = np.fft.irfft(spectrum, n)
    return 0.1 * wave / np.abs(wave).max() * 4


def mics(angle, spacing=doa.SPACING, seed=1):
    """left, right for a voice `angle` degrees off centre (+ = nearer the right mic)."""
    wave = voice(seed=seed)
    delay = spacing * np.sin(np.radians(angle)) / doa.SPEED_OF_SOUND * RATE      # samples, + = left later
    t = np.arange(len(wave))
    left = np.interp(t - delay / 2, t, wave)
    right = np.interp(t + delay / 2, t, wave)
    return left, right


@pytest.mark.parametrize("angle", [-80, -45, -20, 0, 20, 45, 80])
def test_finds_the_angle(angle):
    result = doa.estimate(*mics(angle))
    assert result is not None
    found, clarity = result
    assert abs(found - angle) <= 5 and clarity > doa.CLEAR


def test_flip_mirrors_the_sign():
    left, right = mics(40)
    assert doa.estimate(left, right, flip=True)[0] == pytest.approx(-doa.estimate(left, right)[0])


def test_silence_is_no_voice():
    assert doa.estimate(np.zeros(RATE // 2), np.zeros(RATE // 2)) is None


def test_noise_is_not_clear():
    rng = np.random.default_rng(5)
    left, right = (0.05 * rng.standard_normal(RATE // 2) for _ in range(2))
    found = doa.estimate(left, right)
    assert found is not None and found[1] < doa.CLEAR


def test_combine_median_ignores_unclear_windows():
    assert doa.combine([(30.0, 9.0), (34.0, 8.0), (-70.0, 1.2), (32.0, 7.0)]) == (32.0, 3)
    assert doa.combine([(10.0, 1.0)]) is None


def test_split_stereo_s32():
    raw = np.array([1 << 30, -(1 << 30), 1 << 29, 0], "<i4").tobytes()
    left, right = doa.split(raw)
    assert left.tolist() == [0.5, 0.25] and right.tolist() == [-0.5, 0.0]


def stereo_raw(angle, seconds=1.5):
    """What mic.command() delivers: interleaved 48 kHz S32, a voice `angle` degrees off centre."""
    wave = np.tile(voice(0.5), int(seconds / 0.5))
    delay = doa.SPACING * np.sin(np.radians(angle)) / doa.SPEED_OF_SOUND * RATE
    t = np.arange(len(wave))
    both = np.empty(2 * len(wave), "<i4")
    both[0::2] = np.interp(t - delay / 2, t, wave) * 2 ** 31 * 0.9
    both[1::2] = np.interp(t + delay / 2, t, wave) * 2 ** 31 * 0.9
    return both.tobytes()


def test_turn_for_flips_sign_to_motion_convention():
    assert doa.turn_for(stereo_raw(40)) == pytest.approx(-40, abs=5)      # voice right -> turn right (-)
    assert doa.turn_for(stereo_raw(-30)) == pytest.approx(30, abs=5)


def test_turn_for_stays_when_already_facing_or_unclear():
    assert doa.turn_for(stereo_raw(4)) is None
    assert doa.turn_for(bytes(8 * RATE)) is None


def test_face_speaker_sends_turn_through_the_control_socket():
    from willie.voice import face_speaker
    calls = []
    asked = face_speaker.face(stereo_raw(-50), request=lambda cmd, **kw: calls.append((cmd, kw)) or {"ok": True})
    assert calls[0][0] == "turn" and calls[0][1]["deg"] == asked and asked == pytest.approx(50, abs=6)
    assert face_speaker.face(None, request=lambda *a, **k: calls.append("x")) is None and len(calls) == 1


def test_offset_shifts_the_zero():
    left, right = mics(0)
    assert doa.estimate(left, right, offset=0.0)[0] == pytest.approx(0, abs=3)
    # a fixed lag of 3 samples between the channels reads as ~ 3 samples of angle until corrected
    shifted = np.roll(left, 3)
    biased = doa.estimate(shifted, right)[0]
    assert abs(biased) > 15
    assert doa.estimate(shifted, right, offset=3.0)[0] == pytest.approx(0, abs=4)
