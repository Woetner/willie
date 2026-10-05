"""Radio skill + source switch: one source at a time, same handover as Spotify (no network, no mpv)."""
from __future__ import annotations

import pytest

from willie import music
from willie.skills import radio, spotify
from willie.voice import tools as voice_tools

STATIONS = "Radio 10=http://a/r10; Qmusic=http://a/q; Qmusic Non-stop=http://a/qn; NPO Sterren NL=http://a/s"


class Cfg:
    def __init__(self):
        self.v = {"music.source": "spotify", "music.station": "", "music.radio_volume": 50, "music.stations": STATIONS}

    def get(self, key):
        return self.v[key]

    def reload(self):
        pass

    def update(self, changes):
        for sec, items in changes.items():
            for key, value in items.items():
                self.v[f"{sec}.{key}"] = value


class Proc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    kill = terminate

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = Cfg()
    monkeypatch.setattr(radio, "_config", cfg)
    monkeypatch.setattr(radio, "STATE_DIR", tmp_path)
    monkeypatch.setattr(radio, "START_CHECK_S", 0.0)
    monkeypatch.setattr(radio.shutil, "which", lambda name: "/usr/bin/mpv")
    started = []

    def popen(command, **kw):
        started.append(command[-1])
        return Proc()

    monkeypatch.setattr(radio.subprocess, "Popen", popen)
    monkeypatch.setattr(radio, "_proc", None)
    monkeypatch.setattr(radio, "_current", "")
    for module in (radio, spotify):
        monkeypatch.setattr(module, "TALKING", False)
        monkeypatch.setattr(module, "_resume", False)
        monkeypatch.setattr(module, "_pending", None)
    voice_tools.WRAP_UP.clear()
    # Spotify: a fake that records pause/play and follows a playing flag.
    sp = {"playing": False, "calls": []}
    monkeypatch.setattr(spotify, "configured", lambda: True)
    monkeypatch.setattr(spotify, "playing", lambda: sp["playing"])
    monkeypatch.setattr(spotify, "card_busy", lambda: False)
    monkeypatch.setattr(spotify, "_device_id", lambda: "dev")
    monkeypatch.setattr(spotify, "_find", lambda wat, soort: ("iets", {"context_uri": "x"}))

    def pause():
        sp["calls"].append("pause")
        sp["playing"] = False

    def play(body=None):
        sp["calls"].append("play")
        sp["playing"] = True

    monkeypatch.setattr(spotify, "_pause", pause)
    monkeypatch.setattr(spotify, "_play", play)
    return cfg, started, sp


def test_station_names_are_forgiving(rig):
    assert radio.find("radio 10")[0] == "Radio 10"
    assert radio.find("Q music")[0] == "Qmusic"
    assert radio.find("qmusic non stop")[0] == "Qmusic Non-stop"
    assert radio.find("sterren")[0] == "NPO Sterren NL"
    assert radio.find("")[0] == "Radio 10"            # nothing played yet: the first one
    with pytest.raises(radio.RadioError, match="Ik ken"):
        radio.find("Jazz FM")


def test_speel_radio_switches_the_source_and_stops_spotify(rig):
    cfg, started, sp = rig
    sp["playing"] = True
    result = radio.speel_radio("Qmusic")
    assert result["speelt"] == "Qmusic" and result["start"] == "nu"
    assert started == ["http://a/q"] and radio.playing()
    assert cfg.v["music.source"] == "radio" and cfg.v["music.station"] == "Qmusic"
    assert sp["calls"] == ["pause"]                    # Spotify stopped
    assert radio.speel_radio("")["speelt"] == "Qmusic"  # the last station comes back


def test_spotify_takes_over_from_the_radio(rig):
    cfg, started, sp = rig
    radio.speel_radio("Radio 10")
    result = spotify.speel_muziek()                    # "terug naar Spotify"
    assert result["start"] == "nu"
    assert not radio.playing() and cfg.v["music.source"] == "spotify" and sp["playing"]


def test_a_failed_spotify_request_leaves_the_radio_on(rig, monkeypatch):
    cfg, started, sp = rig
    radio.speel_radio("Radio 10")

    def nothing(wat, soort):
        raise spotify.SpotifyError("Niets gevonden")

    monkeypatch.setattr(spotify, "_find", nothing)
    assert "fout" in spotify.speel_muziek("xyz")
    assert radio.playing() and cfg.v["music.source"] == "radio"


def test_a_dead_stream_is_reported(rig, monkeypatch):
    class Dead(Proc):
        def __init__(self):
            self.alive = False

    monkeypatch.setattr(radio.subprocess, "Popen", lambda *a, **k: Dead())
    assert "geeft geen geluid" in radio.speel_radio("Radio 10")["fout"]


def test_wake_stops_the_stream_and_the_end_resumes_it(rig):
    _, started, _ = rig
    radio.speel_radio("Radio 10")
    music.talking(True)
    music.pause_for_talk()
    assert not radio.playing() and not music.card_busy()
    music.after_talk()
    assert radio.playing() and started == ["http://a/r10", "http://a/r10"]


def test_speel_in_a_conversation_starts_after_it_and_ends_it(rig):
    cfg, started, sp = rig
    music.talking(True)
    music.pause_for_talk()
    result = radio.speel_radio("Qmusic")
    assert "uitgepraat" in result["start"] and voice_tools.WRAP_UP.is_set() and not started
    music.after_talk()
    assert started == ["http://a/q"] and radio.playing()


def test_spotify_resume_is_dropped_when_the_radio_was_asked_for(rig):
    cfg, started, sp = rig
    sp["playing"] = True
    music.talking(True)
    music.pause_for_talk()                             # Spotify paused for talk, wants to resume
    radio.speel_radio("Radio 10")
    music.after_talk()
    assert sp["calls"] == ["pause"] and radio.playing()


def test_hold_pauses_around_speech_and_restarts(rig):
    _, started, _ = rig
    radio.speel_radio("Radio 10")
    with music.hold():
        assert not radio.playing()
    assert radio.playing() and len(started) == 2


def test_sleep_stops_the_radio_for_good(rig):
    radio.speel_radio("Radio 10")
    music.stop()
    music.talking(True)
    music.after_talk()
    assert not radio.playing()


def test_generic_music_tools_follow_the_source(rig):
    cfg, started, sp = rig
    radio.speel_radio("Radio 10")
    assert spotify.muziek_bediening("volgende")["speelt"] == "Qmusic"
    assert spotify.muziek_bediening("vorige")["speelt"] == "Radio 10"
    assert spotify.muziek_volume(30)["muziekvolume"] == 30 and cfg.v["music.radio_volume"] == 30
    assert spotify.wat_speelt_er()["nummer"] == "Radio 10"
    assert spotify.muziek_bediening("pauze")["muziek"] == "radio uit" and not radio.playing()


def test_changing_the_setting_switches_the_playing_source(rig):
    cfg, started, sp = rig
    sp["playing"] = True
    cfg.v["music.source"] = "radio"
    music.sync()
    assert radio.playing() and sp["calls"] == ["pause"]
    cfg.v["music.source"] = "spotify"
    music.sync()
    assert not radio.playing() and sp["playing"]


def test_face_line_shows_the_station(rig):
    radio.speel_radio("Qmusic Non-stop")
    assert music.now_playing() == "Qmusic Non-stop"


def test_song_and_station_take_turns_on_the_face(rig, monkeypatch):
    radio.speel_radio("Qmusic")
    answers = {"metadata/by-key/icy-title": "Adele - Hello"}
    monkeypatch.setattr(radio, "_ipc", lambda *c: answers.get(c[1]))
    clock = [0.0]
    monkeypatch.setattr(radio.time, "monotonic", lambda: clock[0])
    assert music.now_playing() == "Qmusic"
    clock[0] = radio.ALTERNATE_S + 0.1
    assert music.now_playing() == "Adele - Hello"
    assert radio.wat_speelt_er()["artiest"] == "Adele - Hello"
    answers.clear()                                     # no ICY title: just the station
    clock[0] = 2 * radio.ALTERNATE_S + 20
    assert music.now_playing() == "Qmusic"
    answers["media-title"] = "Qmusic_nl_live_96.mp3"    # a file name is not a song
    clock[0] = 100
    assert radio.song() == ""


def test_klank_sets_bass_treble_and_applies_it_live(rig, monkeypatch):
    cfg, started, _ = rig
    cfg.v.update({"music.bass": 0, "music.treble": 0})
    sent = []
    monkeypatch.setattr(radio, "_ipc", lambda *c: sent.append(c))
    assert radio._filter() == ""                                   # neutral: no filter at all
    radio.speel_radio("Radio 10")
    result = radio.radio_klank(bas=4, hoog=-2)
    assert result["bas"] == 4 and result["hoog"] == -2
    assert sent[-1] == ("set_property", "af", "lavfi=[bass=g=4,treble=g=-2,alimiter=limit=0.9]")
    assert radio.radio_klank(preset="stem")["bas"] == -3
    assert radio.radio_klank(bas=99)["bas"] == 8 and "let_op" in radio.radio_klank(bas=8)
    assert "fout" in radio.radio_klank(preset="disco")


def test_a_changed_setting_is_applied_by_the_watcher(rig, monkeypatch):
    cfg, _, _ = rig
    cfg.v.update({"music.bass": 0, "music.treble": 0})
    sent = []
    monkeypatch.setattr(radio, "_ipc", lambda *c: sent.append(c))
    radio.speel_radio("Radio 10")
    music.sync()
    assert not [c for c in sent if c[1] == "af"]
    cfg.v["music.treble"] = 3                                      # dashboard
    music.sync()
    assert sent[-1][1:] == ("af", "lavfi=[bass=g=0,treble=g=3,alimiter=limit=0.9]")
    music.sync()
    assert len([c for c in sent if c[1] == "af"]) == 1             # not re-sent every second
