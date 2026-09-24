"""Spotify skill: music and his voice take turns on the one sound card (no network)."""
from __future__ import annotations

import pytest

from willie.skills import spotify as sp
from willie.voice import tools as voice_tools

REAL_API = sp._api
TRACK = {"name": "Around the World", "uri": "spotify:track:1",
         "artists": [{"name": "Daft Punk"}], "album": {"uri": "spotify:album:9"}}


class Calls(list):
    state = None


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A Spotify that plays on WILL-E and records every call; librespot's state files follow it."""
    monkeypatch.setattr(sp, "STATE_DIR", tmp_path)
    for key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setattr(sp, "TALKING", False)
    monkeypatch.setattr(sp, "_resume", False)
    monkeypatch.setattr(sp, "_pending", None)
    voice_tools.WRAP_UP.clear()
    calls = Calls()

    def state(sink, player):
        (tmp_path / "spotify.sink").write_text(sink)
        (tmp_path / "spotify.player").write_text(player)

    def api(method, path, query=None, body=None, retry=True):
        calls.append((method, path, body))
        if path == "/me/player/devices":
            return {"devices": [{"name": "WILL-E", "id": "dev1"}]}
        if path == "/search":
            return {"tracks": {"items": [TRACK]}}
        if path == "/me/player/pause":
            state("temporarily_closed", "paused")
        if path == "/me/player/play":
            state("running", "playing")
        return None

    monkeypatch.setattr(sp, "_api", api)
    calls.state = state
    return calls


def player_calls(calls):
    return [(m, p, b) for m, p, b in calls if p != "/me/player/devices"]


def test_wake_pauses_and_the_end_resumes(fake):
    fake.state("running", "playing")
    sp.pause_for_talk()
    assert sp.TALKING and not sp.card_busy()
    sp.after_talk()
    assert player_calls(fake) == [("PUT", "/me/player/pause", None), ("PUT", "/me/player/play", None)]
    assert not sp.TALKING


def test_play_in_a_conversation_waits_and_wraps_up(fake):
    sp.pause_for_talk()
    result = sp.speel_muziek("around the world daft punk", "nummer")
    assert result["speelt"] == "Around the World van Daft Punk"
    assert voice_tools.WRAP_UP.is_set()
    assert not any(p == "/me/player/play" for _, p, _ in fake)
    sp.after_talk()
    assert fake[-1] == ("PUT", "/me/player/play",
                        {"context_uri": "spotify:album:9", "offset": {"uri": "spotify:track:1"}})


def test_pause_in_a_conversation_means_no_resume(fake):
    fake.state("running", "playing")
    sp.pause_for_talk()
    sp.muziek_bediening("pauze")
    sp.after_talk()
    assert not any(p == "/me/player/play" for _, p, _ in fake)


def test_sleep_mode_does_not_resume(fake):
    fake.state("running", "playing")
    sp.pause_for_talk()
    sp.after_talk(resume=False)
    assert not any(p == "/me/player/play" for _, p, _ in fake)


def test_speech_outside_a_conversation_pauses_around_it(fake):
    fake.state("running", "playing")
    with sp.hold():
        assert not sp.card_busy()
    assert [p for _, p, _ in player_calls(fake)] == ["/me/player/pause", "/me/player/play"]


def test_from_the_phone_it_plays_at_once(fake):
    assert sp.speel_muziek("x", "nummer")["start"] == "nu"
    assert fake[-1][1] == "/me/player/play"
    assert not voice_tools.WRAP_UP.is_set()


def test_not_linked_is_said_and_hooks_do_nothing(fake, monkeypatch):
    monkeypatch.delenv("SPOTIFY_REFRESH_TOKEN")
    fake.state("running", "playing")
    sp.pause_for_talk()
    sp.after_talk()
    assert fake == []
    monkeypatch.setattr(sp, "_api", REAL_API)      # the real one checks the login first
    assert "make spotify-login" in sp.speel_muziek("x")["fout"]


def test_a_non_json_reply_counts_as_done(monkeypatch):
    """Seen on the Pi 24 Sep: PUT /me/player/pause answers 200 with a body that is not JSON."""
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"ok"
    monkeypatch.setattr(sp, "_access_token", lambda: "t")
    monkeypatch.setattr(sp.urllib.request, "urlopen", lambda *a, **k: Reply())
    assert REAL_API("PUT", "/me/player/pause") is None
