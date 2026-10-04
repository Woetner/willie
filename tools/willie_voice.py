#!/usr/bin/env python3
"""Hands-free WILL-E: wait for the wake word, then talk until it goes quiet.

Run on the Pi:  .venv/bin/python tools/willie_voice.py     (or `make voice-pi`)
Ctrl-C stops it. Nothing stays running afterwards.

Loop: listen for "Hey Willie" -> open a Gemini Live session -> converse ->
close after voice.followup_s (20 s) without words, or when the model says what it
heard was not for him -> save the conversation to memory -> listen again. The wake
listener hands its running microphone to the session, so nothing said after
"Hey Willie" is lost.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from ask_camera import load_env
from willie.audio import chime, speech
from willie.brain import memory
from willie.face.runtime import Face
from willie import control
from willie.voice import face_speaker, garage, gemini_live, wake
from willie.voice.journal import Journal

# Follow-up window: voice.followup_s (20 s) unless WILLIE_IDLE_TIMEOUT overrides it (bench).
IDLE_TIMEOUT = os.environ.get("WILLIE_IDLE_TIMEOUT")


def remember_session(transcript: list, started: datetime, key: str) -> None:
    result = memory.digest(transcript, started, key)
    print(f"memory: {result.get('samenvatting') or result.get('fout') or result.get('reden') or 'saved'}", flush=True)


def muted() -> bool:
    try:
        from willie.config import Config
        return bool(Config().get("privacy.mute"))
    except Exception:
        return False


def music_face(face, spotify) -> None:
    """While Spotify plays on his speaker: the track on the face, and now and then a dance
    (face.dance_every_s on average, face.dance_s long; the face only dances when it rests)."""
    from willie.config import Config
    config, next_dance = Config(), 0.0
    while True:
        if face.dark:                            # deep sleep (Phase P): Spotify is stopped, the screen black
            time.sleep(5)
            continue
        track = spotify.now_playing()
        face.music(track)
        now = time.monotonic()
        if not track:
            next_dance = 0.0
        else:
            try:
                config.reload()
                every, length = float(config.get("face.dance_every_s")), float(config.get("face.dance_s"))
            except Exception:
                every, length = 60.0, 8.0
            if not next_dance:                   # music just started: a first dance soon
                next_dance = now + random.uniform(3, 10)
            if every and now >= next_dance:
                face.dance(length)
                next_dance = now + length + every*random.uniform(0.5, 1.5)
        time.sleep(1)


def run_garage(face, remote, key: str, sleep_now, spotify) -> None:
    """Garage mode (K1): always listening, only to Wouter, until it is switched off."""
    def on_event(kind: str, detail: str) -> None:
        if kind in ("gate", "danger", "go_away", "idle", "error", "wake_again", "tool", "tool_result", "ready"):
            print(f"  [garage] {kind} {detail}".rstrip(), flush=True)

    print("garage mode: listening without the wake word", flush=True)
    spotify.TALKING = True                     # the conversation holds the sound card
    threading.Thread(target=spotify.pause_for_talk, daemon=True).start()
    control.event("wake")
    try:
        garage.Garage(face, remote, key, sleep_now, on_event,
                      remember=lambda t, started: remember_session(t, started, key), muted=muted).run()
    finally:
        threading.Thread(target=spotify.after_talk, args=(False,), daemon=True).start()
    print("garage mode off\n", flush=True)


def main() -> int:
    load_env(REPO / ".env")
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    from willie.voice import talk_key
    gemini_key = talk_key()                  # talking: the free key when set (willie/voice/__init__.py)
    if not gemini_key:
        print("GEMINI_API_KEY missing from .env", file=sys.stderr)
        return 2
    if not wake.available():
        print("pymicro-wakeword missing - run `make deps`", file=sys.stderr)
        return 2

    # systemd stops us with SIGTERM: turn it into the same clean exit as Ctrl-C, so the
    # face gives the console back and the mic is released.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"listening for {wake.describe()} - Ctrl-C to stop")
    face = Face.optional()
    # Phone app bridge (S6): off unless MQTT_HOST is in .env, never blocks this loop.
    from willie.remote import Remote
    from willie.voice import tools as willie_tools
    wake_stop = threading.Event()        # set by the phone app's idle/sleep switch
    sleep_now = threading.Event()        # set by sleep mode: ends a conversation right away
    remote = Remote.from_env(face, wake_stop, sleep_now)
    journal_now: list[Journal] = []      # the running conversation's journal (T0), for the token meter
    from willie import control              # mood events + resting face from the core (G2)
    # Deep sleep (Phase P): the core may switch privacy.mute by itself (idle, night). Follow it within
    # 1 s: stop the wake-word wait, show the sleep face, tell the app - without a chime.
    def follow_sleep_switch():
        from willie.config import Config
        config = Config()                    # one instance: reload() is an mtime check, Config() parses the schema
        while True:
            try:
                config.reload()
                mode = "sleep" if config.get("privacy.mute") else "idle"
            except Exception:
                time.sleep(1)
                continue
            if remote and mode != remote.mode_shown:
                if remote.mode_shown is not None:
                    remote._guard(lambda data: remote._mode(data, chime_on=False), {"mode": mode})
                remote.mode_shown = mode
            elif not remote and mode != follow_sleep_switch.last:
                wake_stop.set()
            follow_sleep_switch.last = mode
            time.sleep(1)
    follow_sleep_switch.last = None
    threading.Thread(target=follow_sleep_switch, name="sleep-switch", daemon=True).start()

    def wake_by_touch():
        """A tap on the sleeping screen = the app's "idle" switch (Phase P)."""
        print("woken by a touch on the screen", flush=True)
        if remote:
            remote._guard(remote._mode, {"mode": "idle"})
            return
        from willie.config import Config
        Config().update({"privacy": {"mute": False}})
        speech.silence(False)
        face.indicators(muted=False)
        face.set_state("idle", "SAY HEY WILLIE")
        wake_stop.set()
        chime.play("idle")

    if face:
        face.on_wake = wake_by_touch
        face.on_pet = lambda: control.event("pet")
        if face.touch:
            willie_tools.CONFIRM = face.confirm  # risky tools need a finger on the glass
        import willie.voice as voice_keys
        voice_keys.KEY_HOOK = lambda paid: face.indicators(paid=paid)   # PAID badge on the face
    # His memory lives on the home server, never on the SD card (Wouter, 24 Sep).
    memory.ON_SERVER = True
    speech.silence(muted())              # started asleep: stay quiet until woken
    if remote:
        willie_tools.FRAME_SOURCE = remote.latest_frame
        willie_tools.POWER_HOOK = remote.powering
        willie_tools.APPROVAL_HOOK = remote.request_approval
        memory.HUB_CALL = remote.hub_call
        from willie.skills import reminders
        reminders.HUB_CALL = remote.hub_call
        from willie.skills import bambu
        bambu.HUB_CALL = remote.hub_call
        from willie.skills import eufy
        eufy.HUB_CALL = remote.hub_call
        from willie.skills import bewaak
        bewaak.HUB_CALL = remote.hub_call
        from willie.skills import garage as garage_skill
        garage_skill.HUB_CALL = remote.hub_call
        from willie.skills import herkennen
        herkennen.HUB_CALL = remote.hub_call
        from willie import missions
        missions.HUB_CALL = remote.hub_call
        from willie.skills import huis
        huis.HUB_CALL = remote.hub_call
        from willie.skills import kennis
        kennis.HUB_CALL = remote.hub_call
        from willie.skills import mail
        mail.HUB_CALL = remote.hub_call
        from willie.skills import muziek
        muziek.HUB_CALL = remote.hub_call
        # Token meter (J3): each session's tokens to the hub, in a thread so a slow server
        # never holds up the next conversation. Server down = that record is lost.
        from willie import usage
        def meter(record):
            if journal_now and record.get("source") == "robot":
                journal_now[0].usage(record)         # the same counts, on the conversation's own record (T0)
            threading.Thread(target=remote.hub_call, args=("gebruik_sessie", record, 5), daemon=True).start()
        usage.HOOK = meter
        # T10: the test sheet once a week (Sunday before dawn), result to the Talk tab.
        def weekly_sheet():
            from willie.voice import sheet
            while True:
                time.sleep(600)
                try:
                    sheet.run(lambda summary: remote.hub_call("g1_rapport", summary, 10),
                              busy=lambda: bool(journal_now) or garage.enabled())
                except Exception as exc:
                    print(f"weekly sheet failed: {exc}", file=sys.stderr, flush=True)
        threading.Thread(target=weekly_sheet, name="weekly-sheet", daemon=True).start()
    from willie.skills import garage as garage_skill
    garage_skill.FACE = face               # pinouts + step plans on the face (K1)
    from willie.skills import zaklamp
    zaklamp.FACE = face                    # flashlight: the screen full white
    from willie import missions
    missions.FACE = face                   # search / adventure / sentry faces (K3-K5)
    # Spotify (willie/skills/spotify.py): music and his voice share one sound card.
    from willie.skills import spotify
    speech.MUSIC = spotify
    if face:
        threading.Thread(target=music_face, args=(face, spotify), name="music-face", daemon=True).start()
    try:
        while True:
            try:
                if muted():
                    # privacy.mute (dashboard or app): no listening at all, not even locally,
                    # and no speaking either.
                    speech.silence(True)
                    if face:
                        face.indicators(mic=False, muted=True)
                        face.set_state("sleep")
                    wake_stop.wait(2)
                    wake_stop.clear()
                    continue
                speech.silence(False)
                if garage.enabled():
                    run_garage(face, remote, gemini_key, sleep_now, spotify)
                    continue
                if face:
                    face.indicators(muted=False)
                    face.waiting()
                    resting = control.request("mood", timeout=0.3).get("face", "idle")
                    if resting != "idle":                    # tired, sad, curious... (G2)
                        face.set_state(resting, "SAY HEY WILLIE")
                    face.indicators(mic=True)
                try:
                    # Re-check mute every 30 s while waiting.
                    wake_stop.clear()
                    recorder = wake.wait_for_wake(stop_after=30, stop=wake_stop)
                finally:
                    if face:
                        face.indicators(mic=False)
                if recorder is None:
                    continue
                if face:
                    face.set_state("curious")
                control.event("wake")
                control.request("event", timeout=0.3, name="conversation_start")
                print("wake!", flush=True)
                if face_speaker.enabled():          # G4: turn towards the voice, off the main loop
                    threading.Thread(target=face_speaker.face, args=(wake.LAST.get("raw"),), daemon=True).start()
                # A local chime instead of a spoken "Ja?" (23 Sep): the cloud TTS took ~1 s
                # and the mic heard it. The recorder keeps running, so a question said
                # straight after "Hey Willie" reaches the model once the session is open.
                # Music pauses first (and lets go of the card), then the chime.
                spotify.TALKING = True
                threading.Thread(target=lambda: (spotify.pause_for_talk(), chime.play("idle")),
                                 daemon=True).start()

                started = time.monotonic()
                session_start = datetime.now()
                first_audio: list[float] = []
                turn_end: list[float] = []
                transcript: list = []

                words = {"heard": "", "said": ""}
                journal = Journal("robot", dict(wake.LAST))
                journal_now[:] = [journal]
                willie_tools.FEEDBACK_HOOK = journal.feedback      # "dat was te lang" -> the turn he judged (T1)

                def on_event(kind: str, detail: str) -> None:
                    elapsed = time.monotonic() - started
                    journal.event(kind, detail)
                    if kind == "user_turn_end":
                        turn_end[:] = [elapsed]
                        control.event("user_speaking")
                    elif kind == "audio" and turn_end:
                        control.event("talking")
                    elif kind == "error":
                        control.event("error")
                    if kind == "audio" and turn_end:
                        # End of Wouter's speech -> first sound back (Gate G1, target < 0.8 s).
                        print(f"  [{elapsed:5.1f}s] reply delay {elapsed - turn_end[0]:.2f} s")
                        turn_end.clear()
                    if kind == "ready":
                        print(f"  [{elapsed:5.1f}s] session open ({detail.split('/')[-1]}) - TALK NOW, he only"
                              f" answers what he hears")
                    elif kind == "audio" and not first_audio:
                        first_audio.append(elapsed)
                        print(f"  [{elapsed:5.1f}s] answering")
                    elif kind == "tool":
                        print(f"  [{elapsed:5.1f}s] tool {detail}")
                    elif kind == "tool_result":
                        print(f"  [{elapsed:5.1f}s]      {detail}")
                    elif kind == "uplink":
                        print(f"  [{elapsed:5.1f}s] mic {detail}")
                    elif kind in ("idle", "error", "interrupted", "standby", "wake_again", "dropped", "local", "stop_word"):
                        print(f"  [{elapsed:5.1f}s] {kind} {detail}".rstrip())
                    # What Gemini heard and said, per turn, first 80 characters (25 Sep:
                    # to see what sets off the repeated answers). Local journal only.
                    if kind in ("heard", "said"):
                        words[kind] += detail
                    elif kind in ("user_turn_end", "turn_complete", "interrupted") and (words["heard"] or words["said"]):
                        for who in ("heard", "said"):
                            if words[who].strip():
                                print(f"  [{elapsed:5.1f}s] {who}: {words[who].strip()[:80]}")
                            words[who] = ""

                if remote:
                    remote.session_active = True
                try:
                    sleep_now.clear()
                    followup = float(IDLE_TIMEOUT) if IDLE_TIMEOUT else gemini_live.configured_followup()
                    # T10: a failed connection is tried once more, and never ends in silence.
                    for attempt in (1, 2):
                        try:
                            asyncio.run(gemini_live.session(gemini_key, idle_timeout=followup, on_event=on_event,
                                                            face=face, cancel=sleep_now,
                                                            recorder=recorder if attempt == 1 else None,
                                                            transcript=transcript))
                            break
                        except RuntimeError as exc:
                            print(f"session failed (try {attempt}): {exc}", file=sys.stderr, flush=True)
                            journal.record["retries"] = attempt
                            if attempt == 2:
                                journal.event("error", str(exc))
                                chime.play("error")
                                try:                     # in words too, when the sentence is already on disk
                                    from willie.voice import local
                                    phrases = local.Phrases(lambda text: (_ for _ in ()).throw(RuntimeError("niet in de cache")),
                                                            Path.home() / ".cache" / "willie" / "phrases")
                                    speech._play_pcm(phrases.pcm(local.NO_LINK), 24000)
                                except Exception:
                                    pass
                                if face:
                                    face.error("GEEN VERBINDING")
                                raise
                finally:
                    control.event("conversation_end")
                    control.request("look", timeout=0.5, pan=0, tilt=0)   # the head back to straight ahead (T8)
                    # Music back on, or what he was asked to play - not after sleep mode.
                    threading.Thread(target=spotify.after_talk, args=(not sleep_now.is_set(),),
                                     daemon=True).start()
                    if remote:
                        remote.session_active = False
                        # T0: every wake and turn to the home server, false wakes included.
                        journal_now.clear()
                        willie_tools.FEEDBACK_HOOK = None
                        threading.Thread(target=remote.hub_call, args=("gesprek_log", journal.finish(), 10),
                                         daemon=True).start()
                    if transcript:
                        # Word-for-word into long-term memory, then merged into facts.md in
                        # the background: the wake word is listening again meanwhile.
                        threading.Thread(target=remember_session, args=(transcript, session_start, gemini_key),
                                         daemon=True).start()
                print("back to sleep\n", flush=True)
            except KeyboardInterrupt:
                print()
                return 0
            except RuntimeError as exc:
                print(f"session failed: {exc}", file=sys.stderr)
                time.sleep(2)
    finally:
        if remote:
            remote.close()
        if face:
            face.close()


if __name__ == "__main__":
    raise SystemExit(main())
