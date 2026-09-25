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
from willie.voice import garage, gemini_live, wake

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
    from willie import control              # mood events + resting face from the core (G2)
    if face:
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
        # Token meter (J3): each session's tokens to the hub, in a thread so a slow server
        # never holds up the next conversation. Server down = that record is lost.
        from willie import usage
        usage.HOOK = lambda record: threading.Thread(
            target=remote.hub_call, args=("gebruik_sessie", record, 5), daemon=True).start()
    from willie.skills import garage as garage_skill
    garage_skill.FACE = face               # pinouts + step plans on the face (K1)
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

                def on_event(kind: str, detail: str) -> None:
                    elapsed = time.monotonic() - started
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
                    elif kind in ("idle", "error", "interrupted", "standby", "wake_again"):
                        print(f"  [{elapsed:5.1f}s] {kind} {detail}".rstrip())

                if remote:
                    remote.session_active = True
                try:
                    sleep_now.clear()
                    followup = float(IDLE_TIMEOUT) if IDLE_TIMEOUT else gemini_live.configured_followup()
                    asyncio.run(gemini_live.session(gemini_key, idle_timeout=followup, on_event=on_event,
                                                    face=face, cancel=sleep_now, recorder=recorder,
                                                    transcript=transcript))
                finally:
                    control.event("conversation_end")
                    # Music back on, or what he was asked to play - not after sleep mode.
                    threading.Thread(target=spotify.after_talk, args=(not sleep_now.is_set(),),
                                     daemon=True).start()
                    if remote:
                        remote.session_active = False
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
