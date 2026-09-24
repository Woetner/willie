"""G3 behaviour selection, with a stand-in body (no MCU needed)."""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

from willie.behavior.tree import Behaviours, in_quiet_hours
from willie.config import defaults, load_schema

DEFAULTS = defaults(load_schema())


class FakeMotion:
    def __init__(self):
        self.calls, self.busy, self.obstacle = [], "", False

    async def move(self, m):
        self.calls.append(("move", round(m, 2)))
        await asyncio.sleep(0)
        return {"ok": not self.obstacle, "reden": "obstakel op 90 mm" if self.obstacle else ""}

    async def turn(self, deg):
        self.calls.append(("turn", round(deg)))
        await asyncio.sleep(0)
        return {"ok": True}

    def stop(self):
        self.calls.append(("stop",))


class FakeMood:
    energy = 0.7

    def snapshot(self):
        return {"energy": self.energy, "curiosity": .7, "happiness": .6, "boredom": .5, "attention": 0}

    def event(self, name):
        return True


def make(hour=14, **settings):
    link = SimpleNamespace(state={"estop": []}, state_t=time.perf_counter(), looks=[])
    link.look = lambda p, t: link.looks.append((p, t))
    body = SimpleNamespace(link=link, safety=SimpleNamespace(battery_state="ok"), motion=FakeMotion(),
                           mood=FakeMood(), conversation=False, last_command=0.0)
    values = {**{f"{s}.{k}": v for s, d in DEFAULTS.items() for k, v in d.items()}, **settings}
    clock = [1000.0]

    async def no_wait(_):
        await asyncio.sleep(0)
    b = Behaviours(body, values.__getitem__, clock=lambda: clock[0],
                   now=lambda: datetime(2026, 9, 24, hour), sleep=no_wait)
    return b, body, clock


def test_quiet_hours_across_midnight():
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 30), "23:00", "07:30")
    assert in_quiet_hours(datetime(2026, 1, 1, 3, 0), "23:00", "07:30")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0), "23:00", "07:30")
    assert in_quiet_hours(datetime(2026, 1, 1, 13, 0), "12:00", "14:00")


def test_priorities():
    b, body, clock = make(**{"behavior.wander": True})
    assert b.choose() == "wander"
    body.mood.energy = 0.1
    assert b.choose() == "nap"
    body.last_command = clock[0] - 10
    assert b.choose() == "respect"
    body.conversation = True
    assert b.choose() == "talk"
    body.link.state["estop"] = ["cliff_l"]
    assert b.choose() == "hold"
    body.link.state = None
    assert b.choose() == "hold"
    night, _, _ = make(hour=2, **{"behavior.wander": True})
    assert night.choose() == "quiet"
    off, _, _ = make()
    assert off.choose() == "idle"


def test_wander_drives_looks_around_and_stops_for_a_conversation():
    async def main():
        b, body, _ = make(**{"behavior.wander": True})
        body.motion.obstacle = True
        assert b.step() == "wander"
        for _ in range(40):
            await asyncio.sleep(0)
        kinds = [c[0] for c in body.motion.calls]
        assert "turn" in kinds and "move" in kinds
        assert body.link.looks[:3] == [(-40, 5), (40, 5), (0, 0)]
        moves = [c[1] for c in body.motion.calls if c[0] == "move"]
        assert all(0 < m <= DEFAULTS["behavior"]["wander_max_m"] for m in moves)
        assert kinds.count("turn") > kinds.count("move")        # turned away from the obstacle
        body.conversation = True
        body.motion.busy = "move"
        assert b.step() == "talk"
        assert ("stop",) in body.motion.calls and b._task is None
    asyncio.run(main())
