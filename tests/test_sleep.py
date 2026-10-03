"""Deep sleep (Phase P): the core follows privacy.mute, falls asleep by itself, and the face
goes black and wakes on a touch. Stand-ins for the MCU link, the Pi helper and the clock."""
from datetime import datetime
from types import SimpleNamespace

from willie.config import defaults, load_schema
from willie.face.framebuffer import Framebuffer
from willie.face.runtime import Face
from willie.hal.link import ST_FIELDS, parse_state
from willie.sleep import Sleep

DEFAULTS = {f"{section}.{key}": value for section, values in defaults(load_schema()).items()
            for key, value in values.items()}


class FakeConfig:
    def __init__(self, **overrides):
        self.values = {**DEFAULTS, **overrides}

    def get(self, key):
        return self.values[key]

    def update(self, nested):
        for section, values in nested.items():
            for key, value in values.items():
                self.values[f"{section}.{key}"] = value


class FakeLink:
    def __init__(self):
        self.stats = SimpleNamespace(connected=True)
        self.sent = []

    def sleep(self, on):
        self.sent.append(("sleep", on))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(hour=14, minute=0, **overrides):
    cfg, link, clock = FakeConfig(**overrides), FakeLink(), Clock()
    now = {"dt": datetime(2026, 10, 2, hour, minute)}
    body = SimpleNamespace(conversation=False, mission=False, motion=SimpleNamespace(busy=""),
                           last_activity=clock())
    runs = []
    sleep = Sleep(cfg, link, body, clock=clock, now=lambda: now["dt"], run=runs.append)
    return sleep, cfg, link, clock, body, runs, now


def test_switch_drives_mcu_and_pi_once_per_change():
    sleep, cfg, link, _, _, runs, _ = make()
    sleep.tick()
    assert link.sent == [("sleep", False)] and runs == ["wake"]      # first tick: brought in line
    cfg.update({"privacy": {"mute": True}})
    sleep.apply()
    sleep.apply()
    assert link.sent[-1] == ("sleep", True) and runs == ["wake", "sleep"]


def test_falls_asleep_after_idle_but_not_while_busy():
    sleep, cfg, _, clock, body, _, _ = make()
    sleep.tick()                                                      # start-up counts as activity
    clock.t += 14 * 60
    sleep.tick()
    assert not cfg.get("privacy.mute")
    clock.t += 2 * 60
    body.conversation = True
    sleep.tick()
    assert not cfg.get("privacy.mute")                                # a conversation keeps him awake
    body.conversation = False
    sleep.tick()
    assert cfg.get("privacy.mute")


def test_garage_mode_and_auto_off_keep_him_awake():
    for overrides in ({"modes.garage": True}, {"sleep.auto": False}):
        sleep, cfg, _, clock, _, _, _ = make(**overrides)
        sleep.tick()
        clock.t += 60 * 60
        sleep.tick()
        assert not cfg.get("privacy.mute")


def test_night_always_sleeps_and_morning_wakes_him():
    sleep, cfg, _, clock, body, runs, now = make(hour=22, minute=59)
    sleep.tick()
    now["dt"] = datetime(2026, 10, 2, 23, 0)
    sleep.tick()
    assert cfg.get("privacy.mute")                                    # at quiet_start, not after 15 min
    # woken by the app at night: asleep again after night_idle_min (5), not 15
    cfg.update({"privacy": {"mute": False}})
    sleep.apply()
    clock.t += 5 * 60
    sleep.tick()
    assert cfg.get("privacy.mute")
    now["dt"] = datetime(2026, 10, 3, 7, 30)
    sleep.tick()
    assert not cfg.get("privacy.mute") and runs[-1] == "wake"


def test_manual_sleep_before_night_is_not_undone_in_the_morning():
    sleep, cfg, _, _, _, _, now = make(hour=22)
    cfg.update({"privacy": {"mute": True}})
    sleep.tick()
    now["dt"] = datetime(2026, 10, 2, 23, 30)
    sleep.tick()
    now["dt"] = datetime(2026, 10, 3, 8, 0)
    sleep.tick()
    assert cfg.get("privacy.mute")


def test_mcu_restart_is_put_back_to_sleep():
    sleep, cfg, link, *_ = make()
    cfg.update({"privacy": {"mute": True}})
    sleep.on_hello()
    assert link.sent == [("sleep", True)]


def test_state_line_reports_asleep_bit():
    words = ["st"] + ["0"] * len(ST_FIELDS)
    words[1 + ST_FIELDS.index("flags")] = "8000"
    assert parse_state(words)["asleep"]


def test_face_goes_black_after_dim_time_and_a_tap_wakes(monkeypatch):
    fb = Framebuffer.canvas()
    face = Face([fb], autostart=False)
    face.sleep_settings = {"dim_after_s": 10, "wake_touch": "tap", "backlight_gpio": 0}
    face.indicators(muted=True)
    face.set_state("sleep")
    assert not face._sleep_dark(100.0)
    assert face._sleep_dark(110.0)
    woken = []
    face.on_wake = lambda: woken.append(1)
    face.sleep_settings["wake_touch"] = "hold"
    face._wake_touch([("tap", 10, 10)])
    face._wake_touch([("long", 10, 10)])
    import time
    time.sleep(0.05)                                                   # on_wake runs on its own thread
    assert woken == [1]
