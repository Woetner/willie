"""G2 mood engine: drift, events, boredom, attention decay, outputs."""
from datetime import datetime

from willie.behavior.mood import NAMES, Mood, energy_clock, load_weights
from willie.config import defaults, load_schema

DEFAULTS = defaults(load_schema())


def make(hour=12):
    t = [0.0]
    get = lambda d: DEFAULTS[d.split(".")[0]][d.split(".")[1]]   # noqa: E731
    mood = Mood(get, clock=lambda: t[0], now=lambda: datetime(2026, 9, 24, hour))
    return mood, t


def test_weights_file_uses_only_known_moods():
    weights = load_weights()
    assert "wake" in weights and "pet" in weights
    assert all(set(d) <= set(NAMES) for d in weights.values())


def test_events_jump_and_values_stay_in_range():
    mood, _ = make()
    before = mood.snapshot()
    assert mood.event("pet")
    after = mood.snapshot()
    assert after["happiness"] > before["happiness"] and after["attention"] > before["attention"]
    for _ in range(20):
        mood.event("pet")
    assert all(0 <= v <= 1 for v in mood.snapshot().values())
    assert not mood.event("no_such_event")


def test_drift_back_boredom_rises_attention_fades():
    mood, t = make()
    mood.event("wake")
    mood.event("bumper.hit")
    unhappy = mood.snapshot()["happiness"]
    t[0] += 60 * 60                                     # an hour with nothing happening
    later = mood.snapshot()
    assert unhappy < later["happiness"] <= DEFAULTS["mood"]["happiness_base"] + 1e-9
    assert later["boredom"] > 0.9
    assert later["attention"] < 0.01
    mood.event("person_seen")
    assert mood.snapshot()["boredom"] < later["boredom"]


def test_energy_follows_the_clock():
    assert energy_clock(9) > energy_clock(15) > energy_clock(21) > energy_clock(23.5)
    night, t = make(hour=23)
    t[0] += 6 * 3600
    assert night.snapshot()["energy"] < DEFAULTS["mood"]["energy_base"] - 0.2


def test_outputs_for_ai_and_face():
    mood, _ = make()
    assert mood.context(64, "garage").startswith("stemming: ")
    assert "batterij 64 %" in mood.context(64) and "kamer: garage" in mood.context(64, "garage")
    for _ in range(3):
        mood.event("pet")
    assert mood.face() in ("happy", "curious")
    assert "vrolijk" in mood.words() or "alert" in mood.words()
    mood.values["energy"] = 0.05
    assert mood.face() == "sleep"
    mood.values.update(energy=.6, happiness=.1, attention=0, curiosity=.5)
    assert mood.face() == "sad" and "mistroostig" in mood.words()
