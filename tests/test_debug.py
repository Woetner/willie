"""Debug tab: arming, clamping, battery refusal, behaviour hold, and the two safety switches."""
import asyncio
import time
from types import SimpleNamespace

from willie.config import defaults, load_schema
from willie.control import Body
from willie.safety import Safety

DEFAULTS = defaults(load_schema())


def get(dotted, overrides=None):
    if overrides and dotted in overrides:
        return overrides[dotted]
    sec, key = dotted.split(".")
    return DEFAULTS[sec][key]


class FakeLink:
    def __init__(self):
        self.sent, self.stopped = [], 0
        self.state = {"ms": 1000, "ticks_l": 0, "ticks_r": 0, "estop": [], "mv": 12000, "ma": 200}
        self.stats = SimpleNamespace(connected=True)
        self.state_t = time.perf_counter()

    def pwm(self, left, right):
        self.sent.append((left, right))

    def stop(self):
        self.stopped += 1


def body(overrides=None, battery="ok"):
    link = FakeLink()
    safety = SimpleNamespace(get=lambda k: get(k, overrides), battery_state=battery)
    b = Body(link, safety, SimpleNamespace(stop=lambda: None, busy=None), SimpleNamespace(snapshot=lambda: {}))
    return b, link


def run(b, **req):
    return asyncio.run(b.handle(req))


def test_not_armed_refuses_and_sends_nothing():
    b, link = body()
    assert "not armed" in run(b, cmd="debug_drive", l=20, r=20)["fout"]
    assert link.sent == [] and not b.debug_active


def test_armed_drives_clamped_and_holds_the_behaviours():
    b, link = body()
    assert run(b, cmd="debug_arm", on=True)["armed"]
    cap = get("drive.pwm_cap")
    out = run(b, cmd="debug_drive", l=90, r=-90)
    assert out["l"] == cap and out["r"] == -cap and link.sent[-1] == (cap, -cap)
    assert b.debug_active
    assert run(b, cmd="debug_drive", l=0, r=0)["ok"] and link.stopped == 1
    run(b, cmd="stop")
    assert not b.debug_active


def test_disarm_stops_and_blocks_again():
    b, link = body()
    run(b, cmd="debug_arm", on=True)
    run(b, cmd="debug_arm", on=False)
    assert link.stopped == 1
    assert "not armed" in run(b, cmd="debug_drive", l=10, r=10)["fout"]


def test_armed_but_battery_empty_refuses():
    b, link = body(battery="cutoff")
    run(b, cmd="debug_arm", on=True)
    assert "battery" in run(b, cmd="debug_drive", l=10, r=10)["fout"]
    assert link.sent == []


def test_debug_state_has_the_live_numbers():
    b, link = body()
    st = run(b, cmd="debug_state")
    assert st["link"] and st["armed"] is False and st["ma"] == 200 and st["ticks_l"] == 0


def test_front_tof_brake_can_be_switched_off():
    near = {"estop": [], "ok": {"tof_c": True}, "tof_l": 0, "tof_c": 60, "tof_r": 0, "mv": 12400}
    assert Safety(get).gate(.2, 0, near, 0).v == 0
    free = Safety(lambda k: get(k, {"safety.tof_brake": False})).gate(.2, 0, near, 0)
    assert free.v == .2 and not free.reason


def test_cliff_switch_off_clears_an_old_cliff_stop_but_nothing_else():
    off = {"safety.cliff_stop": False}
    b, link = body(off)
    link.sent_cmds = []
    link.send = lambda *a: link.sent_cmds.append(a)
    run(b, cmd="debug_arm", on=True)
    link.state["estop"] = ["cliff_l"]
    run(b, cmd="debug_drive", l=20, r=20)
    assert ("clear",) in link.sent_cmds
    link.sent_cmds.clear()
    link.state["estop"] = ["cliff_l", "tilt"]
    run(b, cmd="debug_drive", l=20, r=20)
    assert link.sent_cmds == []                        # a tilt stop stays until Clear stop is pressed
    on, link2 = body()
    link2.send = lambda *a: link2.sent.append(("cmd",) + a)
    run(on, cmd="debug_arm", on=True)
    link2.state["estop"] = ["cliff_l"]
    run(on, cmd="debug_drive", l=20, r=20)
    assert not any(x[0] == "cmd" for x in link2.sent)  # cliff stop on: never cleared by the debug drive
