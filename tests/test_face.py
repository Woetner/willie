"""Offline regression proof for screen ownership, privacy, audio timing and SPI writes."""
import array
import asyncio
import os
import struct
import time
from dataclasses import replace

import pytest

from willie.face import font
from willie.face.framebuffer import Framebuffer
from willie.face.renderer import Renderer, STATES, View
from willie.face.runtime import Face, Touch


class Clock:
    def __init__(self):
        self.now = 100.0
    def __call__(self):
        return self.now


@pytest.fixture
def face():
    return Face(clock=Clock(), autostart=False)


def test_voice_cycle_and_privacy(face):
    face.event("ready")
    assert face.snapshot().state == "listening"
    assert not face.snapshot().mic  # connection is not capture
    face.event("mic_active")
    face.event("user_speaking")
    face.event("user_turn_end")
    assert face.snapshot().state == "thinking" and face.snapshot().mic
    face.event("speaking")
    assert face.snapshot().state == "talking"
    face.event("mic_idle")
    assert not face.snapshot().mic
    face.event("closed")
    v = face.snapshot()
    assert v.state == "idle" and not v.mic and not v.camera and not v.connected


def test_audio_envelope_follows_playback_and_holds_through_tail(face):
    pcm = array.array("h", [12000]*4800).tobytes()
    face.audio(pcm, starts_at=101.0)
    face.event("turn_complete")  # network is done; speaker has not started yet
    assert face.snapshot().state == "talking"
    assert face.snapshot().level == 0
    face.clock.now = 101.1
    assert face.snapshot().level > .5
    face.clock.now = 101.21
    assert face.snapshot().state == "waiting" and face.snapshot().level == 0


def test_interruption_clears_pending_audio_and_never_bounces_later(face):
    face.audio(array.array("h", [15000]*24000).tobytes(), starts_at=101)
    face.event("interrupted")
    face.clock.now = 101.5
    assert face.snapshot().state == "hearing" and face.snapshot().level == 0
    face.clock.now = 103
    assert face.snapshot().state == "hearing"


def test_show_survives_voice_events_but_not_error(face):
    face.indicators(mic=True, camera=True)
    assert face.show("42")["getoond"] == "42"
    face.event("speaking")
    face.event("turn_complete")
    v = face.snapshot()
    assert v.state == "show" and v.text == "42" and v.mic and v.camera
    face.event("error", "do not expose raw credentials in an error")
    assert face.snapshot().state == "error"
    assert "credentials" not in face.snapshot().code


def test_touch_reaction_restores_previous_state_and_preserves_status(face):
    face.set_state("curious")
    face.indicators(mic=True)
    face.pet()
    assert face.snapshot().state == "happy" and face.snapshot().mic
    face.clock.now += 1.5
    assert face.snapshot().state == "curious"
    face.set_state("low_battery")
    face.pet()
    assert face.snapshot().state == "low_battery"


def test_card_pages_can_be_turned_and_all_text_is_reachable(face):
    text = "\n".join(f"LINE {n}" for n in range(30))
    face.show(text)
    assert face.snapshot().page == 0
    face.pet()
    assert face.snapshot().page == 1
    face.clock.now += 6.1
    assert face.snapshot().page == 2
    face.clock.now += 30
    assert face.snapshot().state != "show"


def test_single_card_touch_dismisses(face):
    face.show("42")
    face.pet()
    assert face.snapshot().state != "show"


def test_unknown_telemetry_is_not_invented(face):
    assert face.snapshot().battery is None and face.snapshot().connected is None
    face.indicators(battery=101)
    assert face.snapshot().battery == 100
    face.indicators(battery=None)
    assert face.snapshot().battery is None


@pytest.mark.parametrize("state", STATES)
def test_every_expression_renders_in_bounded_framebuffer(state):
    fb = Framebuffer.canvas()
    renderer = Renderer()
    for i in range(12):
        renderer.draw(fb, View(state=state,text="42",mic=True,camera=True), i*.04)
    assert len(fb.memory) == 480*320*2
    assert any(fb.memory)


def test_static_rows_are_not_sent_repeatedly():
    display = Framebuffer.canvas()
    buffer = display.back_buffer()
    renderer = Renderer()
    view = View()
    renderer.draw(buffer, view, 10)
    assert display.present(buffer) > 0
    assert display.present(buffer) == 0
    renderer.draw(buffer, view, 10.04)
    assert display.present(buffer) < 160  # header/footer should remain untouched
    assert display.memory == buffer.memory


def test_incremental_repaint_matches_a_full_repaint():
    """D6: only changed rows are repainted. Every frame must still equal a fresh full
    render of the same view, in every state, through state changes, blinks and text."""
    incremental, buffer = Renderer(), Framebuffer.canvas()
    skipped = 0
    for i in range(len(STATES)*25):
        state = STATES[i//25]
        now = 100+i*.04
        view = View(state=state, text="M3 = 0.5 MM" if i % 50 < 25 else "", level=(i % 7)/7,
                    battery=15 if i % 3 else 80, mic=i % 11 < 5,
                    watched=state == "idle" and i % 25 > 20, pet=.5 if i % 13 == 0 else 0)
        fresh = Renderer()   # same animation state as the incremental one before this frame
        fresh.pose, fresh.eye_colour, fresh.last_time = list(incremental.pose), incremental.eye_colour, incremental.last_time
        skipped += incremental.draw(buffer, view, now) == 0
        full = Framebuffer.canvas()
        fresh.draw(full, view, now)
        assert buffer.memory == full.memory, f"frame {i} ({state}) differs from a full repaint"
    assert skipped > 0   # static states really skip frames


def test_frame_layout_mismatch_is_rejected():
    with pytest.raises(ValueError,match="layout mismatch"):
        Framebuffer.canvas().present(Framebuffer.canvas(320,480))


def test_32_bit_back_buffer_preserves_layout_and_padding():
    fb = Framebuffer("memory", -1, bytearray(48), 3, 3, 16, 4, (16,8), (8,8), (0,8))
    back = fb.back_buffer()
    back.rect(1,1,1,1,(255,128,64))
    assert fb.present(back) == 1
    assert fb.memory[20:24] == bytes((64,128,255,0))
    assert fb.memory[28:32] == bytes(4)


def test_text_wrap_preserves_long_words_and_engineering_symbols():
    text = "A"*80+"\nM3 = 0.5 mm · 10 µF · 100 Ω · 20°C"
    wrapped = font.lines(text,36)
    assert all(len(line)<=36 for line in wrapped)
    assert "A"*80 in "".join(wrapped)
    assert "Ω" in " ".join(wrapped) and "µF" in " ".join(wrapped)      # real symbols since the glyph font


def test_touch_reads_release_even_when_input_events_are_split(monkeypatch):
    touch = Touch.__new__(Touch)
    touch.fd, touch.pending, touch.down = 0, b"", False
    press = Touch.EVENT.pack(0,0,1,330,1)
    release = Touch.EVENT.pack(0,0,1,330,0)
    chunks = iter((press[:7], press[7:]+release))
    monkeypatch.setattr(os,"read",lambda *_:next(chunks))
    assert not touch.poll()
    assert touch.poll()


def test_render_thread_closes_idempotently():
    fb = Framebuffer.canvas()
    face = Face([fb])
    time.sleep(.12)
    face.close()
    face.close()
    assert not face._thread.is_alive() and face.failure is None
    assert not face.snapshot().mic


def test_show_tool_uses_existing_owner_and_camera_flag_clears(monkeypatch,face):
    from willie.voice import gemini_live
    calls=[]
    def call(name,args):
        assert face.snapshot().camera
        calls.append(name)
        raise RuntimeError("camera failed")
    monkeypatch.setattr(gemini_live.willie_tools,"call",call)
    async def run():
        tools={t.name:t for t in gemini_live.willie_tool_list(face)}
        assert tools["toon"].handler({"tekst":"42"})["getoond"] == "42"
        face._show_until=0
        with pytest.raises(RuntimeError,match="camera failed"):
            await tools["kijk"].handler({})
        assert not face.snapshot().camera
    asyncio.run(run())
    assert calls == ["kijk"]


def test_low_battery_warning_overrides_pet_but_charging_relaxes_it(face):
    face.indicators(battery=15)
    face.pet()
    assert face.snapshot().state == "low_battery"
    face.indicators(charging=True)
    assert face.snapshot().state == "happy"


def test_live_runner_releases_owned_face_on_failed_connection(monkeypatch):
    from willie.voice import gemini_live
    face=Face(autostart=False)
    monkeypatch.setattr(Face,"optional",classmethod(lambda cls:face))
    async def fail(*args,**kwargs):
        raise RuntimeError("offline test connection failure")
    monkeypatch.setattr(gemini_live.GeminiLiveAdapter,"start_session",fail)
    monkeypatch.setattr(gemini_live,"system_prompt",lambda *_:"")
    monkeypatch.setattr(gemini_live,"live_context",lambda:"")
    monkeypatch.setattr(gemini_live,"configured_language",lambda:"nl")
    with pytest.raises(RuntimeError,match="offline test"):
        asyncio.run(gemini_live.session("test"))
    assert face._closed and not face.snapshot().mic


def test_live_runner_uses_borrowed_face_and_leaves_owner_to_close(monkeypatch):
    from willie.voice import gemini_live
    from willie.voice.fake import FakeAdapter
    face=Face(autostart=False)
    fake=FakeAdapter()
    fake.model="offline-fake"
    monkeypatch.setattr(gemini_live,"GeminiLiveAdapter",lambda *_,**kw:fake)
    monkeypatch.setattr(gemini_live,"system_prompt",lambda *_:"")
    monkeypatch.setattr(gemini_live,"live_context",lambda:"")
    monkeypatch.setattr(gemini_live,"configured_language",lambda:"nl")
    async def mic(adapter,speaker,stop,activity,emit,*_):
        emit("mic_active","")
        assert face.snapshot().mic
        emit("user_speaking","")
        emit("user_turn_end","")                     # quiet, but no words heard: a cough
        assert face.snapshot().state!="thinking"
        emit("heard","hoe laat")                     # the server heard words: now thinking
        assert face.snapshot().state=="thinking"
        emit("mic_idle","")
    monkeypatch.setattr(gemini_live,"_microphone",mic)
    assert asyncio.run(gemini_live.session("test",seconds=.1,face=face))=="offline-fake"
    assert not face._closed and not face.snapshot().mic and face.snapshot().state=="idle"
    face.close()


def test_busy_shows_researching_except_while_talking(face):
    face.event("ready")
    with face.busy("RESEARCHING..."):
        view = face.snapshot()
        assert view.state == "thinking" and view.label == "RESEARCHING..."
        face.event("speaking")                       # "Even opzoeken" still shows as talking
        assert face.snapshot().state == "talking"
        face.event("turn_complete")
        face.event("interrupted")                    # back to listening -> busy wins again
        assert face.snapshot().label == "RESEARCHING..."
    assert face.snapshot().label == "" and face.snapshot().state == "hearing"


def test_busy_can_show_the_camera(face):
    face.event("ready")
    with face.busy("LOOKING...", "seeing"):
        view = face.snapshot()
        assert view.state == "seeing" and view.label == "LOOKING..."
    assert face.snapshot().state == "listening"


def test_dance_only_with_music_and_only_over_a_resting_face(face):
    face.dance(8)
    assert face.snapshot().state == "idle"                 # no music: no dance
    face.music("Bohemian Rhapsody - Queen")
    face.dance(8)
    assert face.snapshot().state == "dancing"
    assert face.snapshot().music == "Bohemian Rhapsody - Queen"
    face.set_state("listening")
    assert face.snapshot().state == "listening"            # talking/listening always wins
    face.set_state("idle")
    face.clock.now += 9
    assert face.snapshot().state == "idle"                 # the dance ends by itself
    face.indicators(watched=True)
    face.dance(8)
    assert face.snapshot().state == "watched"              # privacy frame beats the dance (D17)


def test_now_playing_line_renders():
    fb = Framebuffer.canvas()
    Renderer().draw(fb, View(state="dancing", music="Around the World - Daft Punk"), 1.0)
    assert any(fb.memory)


def test_activity_badges_show_real_jobs_only():
    from datetime import datetime, timedelta
    from willie.face.runtime import activity_badges
    now = time.time()
    soon = (datetime.now().astimezone() + timedelta(minutes=12)).isoformat()
    later = (datetime.now().astimezone() + timedelta(days=3)).isoformat()
    snap = {"background": [
        {"id": "printer", "icon": "printer", "progress": .42, "level": "ok"},
        {"id": "eufy", "icon": "eye", "level": "ok"},
        {"id": "w1", "icon": "eye", "level": "warn"},
        {"id": "reminder", "icon": "bell", "at_iso": later, "level": "ok"},
        {"id": "r2", "icon": "bell", "at_iso": soon, "level": "warn"},
        {"id": "x", "icon": "rocket", "level": "ok"},
    ]}
    assert activity_badges(snap, now) == [("printer", "42%", "ok"), ("eye", "", "warn"), ("bell", "12M", "warn")]
    assert activity_badges(None) == [] and activity_badges({}) == []


def test_badges_render_and_never_reach_the_camera_flag(face):
    face.activity([("printer", "100%", "ok"), ("search", "", "ok"), ("eye", "", "bad"),
                   ("bell", "59M", "warn"), ("timer", "5M", "ok")])
    assert len(face.snapshot().badges) == 4
    fb = Framebuffer.canvas()
    Renderer().draw(fb, replace(face.snapshot(), mic=True, camera=True), 0)
    empty = Framebuffer.canvas()
    Renderer().draw(empty, View(mic=True, camera=True), 0)
    assert fb.memory != empty.memory
    # Nothing drawn right of the badge area on the flag row except what the plain frame has.
    for y in range(49, 72):
        row = slice(y*fb.stride + Renderer.BADGE_X1*2, y*fb.stride + 334*2)
        assert fb.memory[row] == empty.memory[row]
    face.activity([])
    assert face.snapshot().badges == ()


CARDS = [
    {"soort": "punten", "titel": "Slaap", "regels": ["Vaste opstaantijd", "7 tot 8 uur", "Geen scherm het laatste uur"]},
    {"soort": "stappen", "titel": "Carburateur", "regels": ["Benzinekraan dicht", "Vlotterkamer los", "Sproeier doorblazen"]},
    {"soort": "vergelijk", "titel": "Langer of regelmatiger",
     "links": {"kop": "Langer", "regels": ["helpt onder 7 uur", "daarboven weinig winst"]},
     "rechts": {"kop": "Vaste tijden", "regels": ["klok loopt gelijk", "minder hart- en vaatziekten"]}},
    {"soort": "getal", "titel": "Slaapduur", "waarde": "7-9 uur", "regels": ["voor volwassenen"]},
    {"soort": "staaf", "titel": "Slaap per nacht", "eenheid": " u",
     "staven": [{"naam": "ma", "waarde": 6.5}, {"naam": "di", "waarde": 7.2}, {"naam": "wo", "waarde": 5.9}]},
]


@pytest.mark.parametrize("args", CARDS, ids=[c["soort"] for c in CARDS])
def test_cards_build_show_render_and_a_tap_returns(face, args):
    from willie.face import card
    built = card.build(args)
    assert isinstance(built, dict)
    assert face.show_card(built) == {"getoond": args["titel"]}
    view = face.snapshot()
    assert view.state == "show" and view.card == built and face.showing()
    plain, drawn = Framebuffer.canvas(), Framebuffer.canvas()
    Renderer().draw(plain, replace(view, card=None), 1.0)
    Renderer().draw(drawn, view, 1.0)
    assert bytes(plain.memory) != bytes(drawn.memory)
    face.pet()
    assert face.snapshot().state != "show"
    face.show("gewone tekst")
    assert face.snapshot().card is None                   # a later plain text is not drawn as the old card


def test_card_says_what_is_missing():
    from willie.face import card
    assert "regels" in card.build({"soort": "punten", "titel": "x"})
    assert "links" in card.build({"soort": "vergelijk", "titel": "x", "rechts": {"kop": "b", "regels": ["1"]}})
    assert "soort" in card.build({"soort": "taart"})
    assert "getal" in card.build({"soort": "staaf", "staven": [{"naam": "a", "waarde": "veel"}]})


def test_captions_follow_his_words_and_card_buttons_reach_the_session(face):
    """T6: live captions while talking; MEER / HERHAAL on a card call on_card and keep the card."""
    from willie.face import card
    face.event("ready"); face.event("speaking")
    face.caption("Vaste tijden zijn beter, zolang je aan ongeveer ")
    face.caption("zeven uur slaap komt.")
    view = face.snapshot()
    assert view.state == "talking" and view.caption.endswith("zeven uur slaap komt.")
    plain, drawn = Framebuffer.canvas(), Framebuffer.canvas()
    Renderer().draw(plain, replace(view, caption=""), 1.0)
    Renderer().draw(drawn, view, 1.0)
    assert bytes(plain.memory) != bytes(drawn.memory)
    face.event("turn_complete"); face.event("speaking")
    assert face.snapshot().caption == ""                              # a new answer starts clean

    got = []
    face.touch, face.on_card = object(), got.append
    face.show_card(card.build(CARDS[0]))
    assert not face._card_tap(face.clock(), [("tap", 100.0, 150.0)])      # the card itself: not a button
    assert face._card_tap(face.clock(), [("tap", 290.0, 298.0)])          # MEER
    for _ in range(50):
        if got:
            break
        time.sleep(0.01)
    assert got == ["MEER"] and face.snapshot().state == "show"


def test_buttons_take_a_touch_that_slid_and_calibration_solves_the_panel(face):
    """A resistive panel jitters: a touch that moved past MOVE_PX is a "release", and still presses a button."""
    got = []
    face.touch = type("T", (), {"released_at": (410.0, 270.0), "released_raw": (3000, 2500)})()

    # Four crosses on a panel whose x axis runs backwards: raw 3200 at the left edge, 900 at the right.
    raw = lambda x, y: (3200 - x / 480 * 2300, 580 + y / 320 * 2380)
    values = Face.calibration_from([raw(*p) for p in Face.CALIB_POINTS])
    assert values["touch_flip_x"] and not values["touch_flip_y"] and not values["touch_swap_xy"]
    assert abs(values["touch_x_min"] - 900) <= 2 and abs(values["touch_x_max"] - 3200) <= 2
    assert abs(values["touch_y_min"] - 580) <= 2 and abs(values["touch_y_max"] - 2960) <= 2
    swapped = Face.calibration_from([tuple(reversed(raw(*p))) for p in Face.CALIB_POINTS])
    assert swapped["touch_swap_xy"] and swapped["touch_flip_x"]
    assert Face.calibration_from([(100, 100)] * 4) is None

    face.calibrate()
    assert face.snapshot().state == "calibrate" and face.snapshot().calib == (0, None)
    for point in Face.CALIB_POINTS:
        face.clock.now += 1.0
        face._calibrate_tap(face.clock(), raw(*point))
    assert face.snapshot().calib == (4, True) and face.settings["touch_flip_x"] is True
    face.clock.now += 3.0
    assert face.snapshot().state != "calibrate"


def test_new_font_has_lower_case_accents_and_spaces():
    assert font.normalise("Één café, 10 µF") == "Één café, 10 µF"
    blank, text = Framebuffer.canvas(), Framebuffer.canvas()
    Renderer().draw(blank, View(state="show", text="   "), 1.0)
    Renderer().draw(text, View(state="show", text="a b"), 1.0)
    assert bytes(blank.memory) != bytes(text.memory)


def test_flashlight_is_full_white_ends_on_tap_and_by_itself(face):
    clock = face.clock
    face.flashlight(True, 60)
    assert face.snapshot().state == "flashlight"
    fb = Framebuffer.canvas()
    Renderer().draw(fb, face.snapshot(), 0, {"brightness": 5})
    assert fb.memory[:64] == b"\xff" * 64               # brightness setting does not dim it
    face.flashlight(False)
    assert face.snapshot().state != "flashlight"
    face.flashlight(True, 60)
    clock.now += 61
    assert face.snapshot().state != "flashlight"


def test_every_phase_of_a_turn_has_its_own_face(face):
    face.event("ready")
    assert face.snapshot().state == "listening"            # waiting after the wake word
    face.event("user_speaking")
    assert face.snapshot().state == "hearing"              # you are talking
    face.event("turn_pending")
    assert face.snapshot().state == "understanding"        # quiet, words not read yet
    face.event("user_turn_end")
    assert face.snapshot().state == "thinking"
    face.event("tool", "web_search")
    assert face.snapshot().state == "working"
    face.event("tool_result", "ok")
    assert face.snapshot().state == "thinking"
    face.event("speaking")
    assert face.snapshot().state == "talking"
    face.event("turn_complete")
    face.clock.now += 5
    assert face.snapshot().state == "waiting"              # his turn is over, yours
    face.event("user_speaking")
    face.event("turn_pending")
    face.event("dropped", "noise")
    assert face.snapshot().state == "waiting"              # it was only noise

    # A one-word answer is too short for a turn: sound -> hearing, quiet -> understanding,
    # the server's words keep it there, and with no answer it falls back to waiting.
    face.event("voice_start")
    assert face.snapshot().state == "hearing"
    face.event("voice_stop")
    assert face.snapshot().state == "understanding"
    face.clock.now += Face.UNDERSTAND_S + .1
    assert face.snapshot().state == "waiting"
    face.event("heard", "ja")                              # words read with no local turn: still shown
    assert face.snapshot().state == "understanding"
