"""willie.audio.tune: judging the mics and fitting the direction numbers on synthetic recordings."""
import numpy as np
import pytest

from willie.audio import doa, tune

RATE = doa.CARD_RATE


def stereo(left, right):
    both = np.empty(2 * len(left), "<i4")
    both[0::2] = np.clip(left, -1, 1) * 2 ** 31 * 0.99
    both[1::2] = np.clip(right, -1, 1) * 2 ** 31 * 0.99
    return both.tobytes()


def noise(level, seconds, seed):
    return level * np.random.default_rng(seed).standard_normal(int(seconds * RATE))


def talk(level, seconds=3.0, seed=2):
    n = int(seconds * RATE)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    freq = np.fft.rfftfreq(n, 1 / RATE)
    spectrum[(freq < 200) | (freq > 4000)] = 0
    wave = np.fft.irfft(spectrum, n)
    return level * wave / np.std(wave)


def lagged(wave, lag):
    """left, right with `left` `lag` samples later (a voice nearer the right mic)."""
    t = np.arange(len(wave))
    return np.interp(t - lag / 2, t, wave), np.interp(t + lag / 2, t, wave)


def by_name(checks):
    return {c.name: c for c in checks}


def test_quiet_room_passes():
    raw = stereo(noise(0.001, 2, 1), noise(0.001, 2, 2))
    checks = by_name(tune.check_silence(raw, 2.0, 2.0))
    assert all(c.status == tune.PASS for c in checks.values())


def test_loud_room_and_dropouts_are_flagged():
    raw = stereo(noise(0.02, 2, 1), noise(0.001, 2, 2))
    checks = by_name(tune.check_silence(raw, 2.0, 2.4))
    assert checks["noise floor left"].status == tune.FAIL
    assert checks["noise floor right"].status == tune.PASS
    assert checks["card dropouts"].status == tune.FAIL


def test_speech_checks_snr_balance_and_clipping():
    wave = talk(0.05)
    good = by_name(tune.check_speech(stereo(wave, wave), (0.001, 0.001)))
    assert good["speech over noise left"].status == tune.PASS
    assert good["left/right balance"].status == tune.PASS
    assert good["clipping at the card"].status == tune.PASS
    odd = by_name(tune.check_speech(stereo(wave, wave * 0.25), (0.001, 0.001)))
    assert odd["left/right balance"].status == tune.FAIL
    loud = by_name(tune.check_speech(stereo(talk(1.5), talk(1.5)), (0.001, 0.001)))
    assert loud["clipping at the card"].status == tune.FAIL


def test_gain_lands_the_peak_at_the_target():
    wave = talk(0.02)
    gain, peak = tune.gain_for(stereo(wave, wave))
    assert gain * peak == pytest.approx(tune.TARGET_PEAK, rel=0.01)
    assert tune.gain_for(stereo(np.zeros(RATE), np.zeros(RATE)))[0] == tune.GAIN_RANGE[1]


def positions(spacing, offset, flip=False):
    sign = -1 if flip else 1
    out = []
    for angle in (0.0, 90.0, -90.0):
        lag = sign * spacing * np.sin(np.radians(angle)) / doa.SPEED_OF_SOUND * RATE + offset
        out.append((angle, lag))
    return out


@pytest.mark.parametrize("spacing,offset,flip", [(0.078, 0.0, False), (0.10, 1.5, False), (0.09, -2.0, True)])
def test_fit_recovers_spacing_offset_and_sign(spacing, offset, flip):
    cal, error = tune.fit(positions(spacing, offset, flip))
    assert cal.spacing == pytest.approx(spacing, abs=0.002)
    assert cal.offset == pytest.approx(offset, abs=0.05)
    assert cal.flip is flip
    assert error < 1.0


def test_fit_needs_two_angles_and_a_real_slope():
    assert tune.fit([(0.0, 0.5), (0.0, 0.6)]) is None
    assert tune.fit([(0.0, 0.0), (90.0, 0.2)]) is None


def test_window_lag_reads_a_voice_wider_than_the_default_reach():
    left, right = lagged(talk(0.1, 4.0), 12.0)               # 12 samples = 86 mm * ... beyond SPACING's 11
    lag, windows = tune.window_lag(stereo(left, right))
    assert lag == pytest.approx(12.0, abs=0.7) and windows >= doa.MIN_CLEAR


def test_calibrated_bearing_with_offset_and_flip():
    wave = talk(0.1, 1.5)
    left, right = lagged(wave, doa.SPACING * np.sin(np.radians(40)) / doa.SPEED_OF_SOUND * RATE + 2.0)
    raw = stereo(left, right)
    cal = doa.Calibration(doa.SPACING, 2.0, False)
    assert doa.bearing(raw, cal)[0] == pytest.approx(40, abs=5)
    mirrored = doa.Calibration(doa.SPACING, 2.0, True)
    assert doa.bearing(raw, mirrored)[0] == pytest.approx(-40, abs=5)


def test_score_and_gain_note():
    within, total, checks = tune.score([(0, 5), (30, None), (60, 50), (-30, -80)] * 2 + [(0, 0), (0, 1)])
    assert (within, total) == (6, 10) and checks[0].status == tune.WARN
    assert tune.score([(0, 0)] * 8 + [(0, None)] * 2)[2][0].status == tune.PASS
    assert tune.gain_note(8, 16).status == tune.WARN
    assert tune.gain_note(14, 16).status == tune.PASS
