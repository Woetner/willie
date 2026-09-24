"""K3-K5 missions with a stand-in robot: no MCU, camera, server or face needed."""
import array
import threading
import time

from willie import missions
from willie.missions import sentry as sentry_mod
from willie.missions.adventure import Adventure
from willie.missions.search import Search
from willie.missions.sentry import Sentry


class FakeRobot:
    def __init__(self, hub_answers=None):
        self.calls, self.hub_calls, self.said = [], [], []
        self.pose = {"x_mm": 0, "y_mm": 0, "th_deg": 0.0}
        self.tof = [900, 1500, 900]
        self.hub_answers = hub_answers or {}
        self.face = None
        self.photos = 0
        self.acc = [0, 0, 1000]

    def body(self, cmd, timeout=30.0, **args):
        self.calls.append((cmd, args))
        if cmd == "state":
            return {"pose": dict(self.pose), "tof_mm": self.tof, "battery_v": 12.1, "imu": {"acc_mg": self.acc}}
        if cmd == "turn":
            self.pose["th_deg"] = (self.pose["th_deg"] + args["deg"] + 180) % 360 - 180
        if cmd == "move":
            import math
            th = math.radians(self.pose["th_deg"])
            self.pose["x_mm"] += args["m"] * 1000 * math.cos(th)
            self.pose["y_mm"] += args["m"] * 1000 * math.sin(th)
        return {"ok": True}

    def photo(self):
        self.photos += 1
        return b"\xff\xd8jpeg"

    def hub(self, name, args, timeout=30.0):
        self.hub_calls.append(name)
        answer = self.hub_answers.get(name, {})
        return answer(args) if callable(answer) else answer

    def say(self, text):
        self.said.append(text)

    def sleep(self, seconds, stop):
        return stop.wait(min(seconds, 0.01))


def test_search_finds_on_a_later_stop():
    looks = {"n": 0}

    def kijk(args):
        looks["n"] += 1
        return {"gevonden": looks["n"] == 7, "zeker": 0.9}

    robot = FakeRobot({"missie_kijk": kijk, "waar_is": {"laatst": [{"wanneer": "gisteren", "waar": "keuken"}]}})
    result = Search(robot, naam="de kat").run()
    assert result["gevonden"] and "keuken" in result["laatst_gezien"]
    assert ("move", {"m": 0.8}) in robot.calls and "missie_melding" in robot.hub_calls
    assert robot.said == ["Gevonden: de kat!"]


def test_search_gives_up_and_asks_the_eufy_cameras(monkeypatch):
    monkeypatch.setattr("willie.missions.search.MAX_STOPS", 2)
    robot = FakeRobot({"missie_kijk": {"gevonden": False}, "zoek_buiten": {"gevonden": False}})
    result = Search(robot, omschrijving_en="a red screwdriver").run()
    assert not result["gevonden"] and "zoek_buiten" in robot.hub_calls


def test_adventure_explores_and_tells_the_story(monkeypatch):
    monkeypatch.setattr("willie.missions.adventure._setting",
                        lambda key, default: {"adventure.max_stops": 3}.get(key, default))
    robot = FakeRobot({"avontuur_zie": {"nieuw": 1}, "avontuur_einde": {"verhaal": "Wat een reis!", "kamers": ["gang"]}})
    result = Adventure(robot).run()
    assert result["plekken"] == 3 and result["ontdekkingen"] == 36      # 3 stops x 4 headings x 3 photos
    assert robot.said[-1] == "Wat een reis!"
    moves = [a["m"] for c, a in robot.calls if c == "move"]
    assert len(moves) == 3 and all(0 < m <= 1.0 for m in moves)


def test_adventure_stops_when_boxed_in(monkeypatch):
    monkeypatch.setattr("willie.missions.adventure._setting", lambda key, default: default)
    robot = FakeRobot({"avontuur_einde": {}})
    robot.tof = [100, 200, 100]
    result = Adventure(robot).run()
    assert result["plekken"] == 1 and "vastgelopen" in result["reden"]


def test_sentry_sound_spike_raises_one_alarm():
    robot = FakeRobot({"wacht_status": {"events": []}, "wacht_kijk": {"alarm": False}})
    sentry = Sentry(robot)
    quiet = array.array("h", [300] * 1600).tobytes()
    bang = array.array("h", [30000] * 1600).tobytes()
    thread = threading.Thread(target=sentry.run)
    thread.start()
    time.sleep(0.05)
    for _ in range(30):
        sentry_mod.on_audio(quiet)
    sentry_mod.on_audio(bang)
    sentry_mod.on_audio(bang)                     # a second bang within the cooldown: no second alarm
    time.sleep(0.3)
    sentry.stop_event.set()
    thread.join(2)
    assert robot.hub_calls.count("wacht_alarm") == 1 and sentry.alarms == 1
    assert ("look", {"pan": -70, "tilt": 0}) in robot.calls


def test_sentry_eufy_person_and_bump():
    robot = FakeRobot({"wacht_status": {"events": [{"camera": "Voordeur"}]}, "wacht_kijk": {"alarm": False}})
    sentry = Sentry(robot)
    thread = threading.Thread(target=sentry.run)
    thread.start()
    time.sleep(0.2)
    sentry.stop_event.set()
    thread.join(2)
    assert sentry.alarms == 1


def test_one_mission_at_a_time(monkeypatch):
    robot = FakeRobot({"wacht_status": {}, "wacht_kijk": {}})
    assert missions.start("wacht", robot)["ok"]
    assert "loopt al" in missions.start("wacht", robot)["fout"]
    assert missions.status()["missie"] == "wacht"
    assert missions.stop()["gestopt"] == "wacht"
    assert "onbekende" in missions.start("dans", robot)["fout"]
