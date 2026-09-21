"""willie.audio.mic: left channel only, gain, clipping (the INMP441 is on the left)."""
import array

from willie.audio import mic


def test_left_channel_gain_and_clip():
    stereo = array.array("h", [100, 999, -200, 999, 5000, 999, -5000, 999]).tobytes()
    out = array.array("h", mic.left(stereo, 8.0))
    assert list(out) == [800, -1600, 32767, -32768]
    assert mic.left(stereo[:6], 2.0) == array.array("h", [200]).tobytes()   # partial frame dropped
    assert "-c" in mic.command() and mic.command()[mic.command().index("-c") + 1] == "2"
