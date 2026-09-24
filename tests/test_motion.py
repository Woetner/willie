"""F1-F3 on the Pi side: safety gate (pure) and move/turn/recover against tools/fake_mcu.py."""
import asyncio
import math
import os
import subprocess
import sys
import time

import pytest

from willie.config import defaults, load_schema
from willie.hal.link import Link
from willie.motion import Motion
from willie.safety import Safety

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEFAULTS = defaults(load_schema())


def get(dotted, overrides={}):
    if dotted in overrides:
        return overrides[dotted]
    sec, key = dotted.split(".")
    return DEFAULTS[sec][key]


def state(**kw):
    base = {"estop": [], "ok": {"tof_l": True, "tof_c": True, "tof_r": True, "ina": True},
            "tof_l": 0, "tof_c": 0, "tof_r": 0, "mv": 12400}
    base.update(kw)
    return base


# ---- safety gate, pure ----------------------------------------------------------
def test_gate_refuses_stale_state_and_estop():
    s = Safety(get)
    assert s.gate(.1, 0, None, 0).refused
    assert s.gate(.1, 0, state(), 1.0).refused
    assert "noodstop" in s.gate(.1, 0, state(estop=["bump_l"]), 0).reason


def test_gate_clamps_and_brakes_for_obstacles():
    s = Safety(get)
    stop = get("safety.tof_stop_mm")
    assert s.gate(5, 9, state(), 0).v == get("drive.max_speed")
    far = s.gate(.2, 0, state(tof_c=3 * stop), 0)
    assert far.v == .2 and not far.reason
    slow = s.gate(.2, 0, state(tof_c=int(1.5 * stop)), 0)
    assert 0 < slow.v < .2
    blocked = s.gate(.2, .5, state(tof_c=stop - 10), 0)
    assert blocked.v == 0 and blocked.w == .5 and "obstakel" in blocked.reason
    assert s.gate(-.1, 0, state(tof_c=10), 0).v == -.1          # backing away is fine
    assert s.gate(.2, 0, state(tof_c=10, ok={"tof_c": False}), 0).v == .2   # absent sensor ignored


def test_battery_needs_a_sustained_low_before_poweroff():
    clock = [0.0]
    off, events = [], []
    s = Safety(get, clock=lambda: clock[0], on_poweroff=lambda: off.append(1),
               on_event=lambda n, d: events.append(n))
    low = state(mv=int(get("safety.battery_cutoff_v") * 1000) - 200)
    assert s.battery(low) == "ok"                                # a sag is not a cutoff
    clock[0] = 11
    assert s.battery(low) == "cutoff" and off == [1]
    assert s.gate(.1, 0, low, 0).refused
    s.battery(low)
    assert off == [1]                                            # asked once
    assert "battery.cutoff" in events
    assert Safety(get).battery(state(ok={})) == "unknown"
    bench = Safety(get, clock=lambda: clock[0], on_poweroff=lambda: off.append(2))
    for clock[0] in (0, 20, 40):
        assert bench.battery(state(mv=40)) == "unknown"          # INA219 without a pack
    assert off == [1]


# ---- motion against the fake MCU -------------------------------------------------
@pytest.fixture
def robot(tmp_path):
    port = str(tmp_path / "mcu")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "fake_mcu.py"), port],
                         stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    yield port
    p.kill()


def run(port, body, overrides={}):
    async def main():
        link = Link(port, baud=115200)
        link._open()
        await link.ping(0.5)
        link.send("sim", "tof", 0, 0, 0)
        g = lambda d: get(d, overrides)                          # noqa: E731
        motion = Motion(link, Safety(g), g)
        try:
            return await body(link, motion)
        finally:
            link.close()
    return asyncio.run(main())


def test_move_forward_and_back(robot):
    async def body(link, motion):
        t = time.perf_counter()
        fwd = await motion.move(0.3)
        took = time.perf_counter() - t
        back = await motion.move(-0.1)
        return fwd, back, took
    fwd, back, took = run(robot, body)
    assert fwd["ok"] and abs(fwd["afstand_mm"] - 300) <= 25, fwd
    assert back["ok"] and abs(back["afstand_mm"] + 100) <= 25, back
    assert took < 4


def test_turn_both_ways_and_past_180(robot):
    async def body(link, motion):
        return await motion.turn(90), await motion.turn(-200)
    left, right = run(robot, body)
    assert left["ok"] and abs(left["gedraaid_graden"] - 90) <= 5, left
    assert right["ok"] and abs(right["gedraaid_graden"] + 200) <= 5, right


def test_obstacle_stops_a_move(robot):
    async def body(link, motion):
        async def wall():
            await asyncio.sleep(0.4)
            link.send("sim", "tof", 0, 60, 0)
        asyncio.ensure_future(wall())
        return await motion.move(1.0)
    result = run(robot, body)
    assert not result["ok"] and "obstakel" in result["reden"] and result["afstand_mm"] < 400


def test_estop_mid_move_then_recover_backs_off(robot):
    async def body(link, motion):
        async def bump():
            await asyncio.sleep(0.4)
            link.send("sim", "estop", "bump_l")
        asyncio.ensure_future(bump())
        hit = await motion.move(1.0)
        before = link.state["x_mm"]
        rec = await motion.recover("bump_l")
        return hit, rec, before - link.state["x_mm"], await motion.recover("tilt")
    hit, rec, backed, tilt = run(robot, body)
    assert not hit["ok"] and "noodstop" in hit["reden"]
    assert rec["ok"] and abs(backed - get("safety.bumper_reverse_mm")) <= 20, (rec, backed)
    assert not tilt["ok"]


def test_new_request_interrupts_the_running_one(robot):
    async def body(link, motion):
        first = asyncio.ensure_future(motion.move(1.0))
        await asyncio.sleep(0.3)
        second = await motion.turn(30)
        return await first, second
    first, second = run(robot, body)
    assert first == {"ok": False, "reden": "onderbroken"} and second["ok"]
