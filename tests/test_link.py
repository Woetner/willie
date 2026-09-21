"""Link protocol + safety behaviour against tools/fake_mcu.py (same protocol as the firmware)."""
import asyncio
import os
import subprocess
import sys
import time

import pytest

from willie.hal import proto
from willie.hal.link import ST_FIELDS, Link, parse_state

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_parse_state():
    vals = ["0"] * len(ST_FIELDS)
    vals[ST_FIELDS.index("io")] = "EF"
    vals[ST_FIELDS.index("flags")] = "1E21"          # bump_l, pcf, mpu? no: 0x21 = bit0 + bit5
    st = parse_state(["st", *vals])
    assert st["io"] == 0xEF
    assert st["estop"] == ["bump_l"]
    assert st["ok"]["pcf"] and not st["ok"]["mpu"]
    assert st["ok"]["tof_c"] and st["ok"]["tof_r"] and st["ok"]["enc"] and st["moving"]
    with pytest.raises(proto.BadLine):
        parse_state(["st", "1", "2"])


@pytest.fixture
def fake_port(tmp_path):
    port = str(tmp_path / "mcu")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "fake_mcu.py"), port],
                         stdout=subprocess.PIPE, text=True)
    p.stdout.readline()                              # printed once the pty exists
    yield port
    p.kill()


def test_watchdog_and_cap(fake_port):
    # Wait on conditions, not fixed sleeps: the fake MCU is a Python process and may lag.
    async def run():
        events = []
        link = Link(fake_port, baud=115200, on_message=events.append)   # a macOS pty rejects 921600
        link._open()
        assert await link.ping(0.5) is not None
        link.cfg("stream_hz", 20)

        async def until(pred, timeout=1.0):
            end = time.perf_counter() + timeout
            while not (link.state and pred(link.state)):
                assert time.perf_counter() < end, link.state
                await asyncio.sleep(0.005)

        await until(lambda st: True)
        assert link.clock_offset_ms is not None

        link.pwm(80, -80)                            # above the 50 % hard cap
        t_last = time.perf_counter()
        await until(lambda st: st["pwm_l"] != 0)
        assert (link.state["pwm_l"], link.state["pwm_r"]) == (50, -50) and link.state["moving"]

        await until(lambda st: st["pwm_l"] == 0)     # silence: the watchdog must brake
        stop_ms = (link.state_t - t_last) * 1000
        assert 190 < stop_ms < 320, stop_ms          # 200 ms + up to one 50 ms frame
        assert any(w[:2] == ["ev", "wd"] for w in events), events

        link.look(200, -100)                         # clamped to the configured limits
        await until(lambda st: (st["pan_d10"], st["tilt_servo_d10"]) == (900, -300), timeout=2)
        link.close()

    asyncio.run(run())


def test_mcu_settings_cover_schema():
    from willie.config import defaults, load_schema
    from willie.hal.link import mcu_settings

    d = defaults(load_schema())
    got = mcu_settings(lambda dotted: d[dotted.split(".")[0]][dotted.split(".")[1]])
    assert got["pwm_cap"] == 50 and got["wd_ms"] == 200 and got["pan_c"] == 1500
