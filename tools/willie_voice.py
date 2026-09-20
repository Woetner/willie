#!/usr/bin/env python3
"""Hands-free WILL-E: wait for the wake word, then talk until it goes quiet.

Run on the Pi:  .venv/bin/python tools/willie_voice.py     (or `make voice-pi`)
Ctrl-C stops it. Nothing stays running afterwards.

Loop: listen for "Hey Gemini" -> open a Gemini Live session -> converse ->
close after 30 s of silence -> listen again. Both halves share one microphone,
so the wake listener always releases it before the session opens.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from ask_camera import load_env
from willie.audio import speech
from willie.voice import gemini_live, wake

IDLE_TIMEOUT = float(os.environ.get("WILLIE_IDLE_TIMEOUT", "30"))


def main() -> int:
    load_env(REPO / ".env")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("GEMINI_API_KEY missing from .env", file=sys.stderr)
        return 2
    if not wake.available():
        print(f"Vosk model missing - see config/wakewords/README.md", file=sys.stderr)
        return 2

    print(f"listening for {wake.describe()} - Ctrl-C to stop")
    while True:
        try:
            if not wake.listen_for_wake():
                continue
            print("wake!", flush=True)
            backend = speech.speak("Ja?")
            print(f"  said 'Ja?' via {backend or 'NOTHING - no tts and no espeak-ng'}", flush=True)

            started = time.monotonic()
            first_audio: list[float] = []

            def on_event(kind: str, detail: str) -> None:
                elapsed = time.monotonic() - started
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
                elif kind in ("idle", "error", "interrupted"):
                    print(f"  [{elapsed:5.1f}s] {kind} {detail}".rstrip())

            asyncio.run(gemini_live.session(gemini_key, idle_timeout=IDLE_TIMEOUT, on_event=on_event))
            print("back to sleep\n", flush=True)
        except KeyboardInterrupt:
            print()
            return 0
        except RuntimeError as exc:
            print(f"session failed: {exc}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
