#!/usr/bin/env python3
"""Voice audition (D7): the same Dutch sentences in each candidate Gemini voice.

Run on the Pi (it holds the API key):  .venv/bin/python tools/voice_samples.py [Voice ...]
Writes .local/voices/<n>_<Voice>.wav (kept between runs, so a quota stop can be resumed with
just the missing names); `make voices` fetches them and plays them on the Mac.

The Live API uses the same 30 prebuilt voices as Gemini TTS, so a TTS sample is a fair
preview of how he will sound in a conversation.
"""

from __future__ import annotations

import os
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from ask_camera import load_env
from willie.audio import speech

# Male-sounding voices, calmest first (Google's one-word labels; gender by ear, Google does not list it).
CANDIDATES = {
    "Charon": "informative (current)",
    "Algieba": "smooth",
    "Iapetus": "clear",
    "Schedar": "even",
    "Orus": "firm",
    "Alnilam": "firm",
    "Rasalgethi": "informative",
    "Sadaltager": "knowledgeable",
    # The rest of the male-sounding voices, for a full comparison (21 Sep, Wouter).
    "Puck": "upbeat",
    "Fenrir": "excitable",
    "Enceladus": "breathy",
    "Umbriel": "easy-going",
    "Algenib": "gravelly",
    "Achird": "friendly",
    "Zubenelgenubi": "casual",
    "Sadachbia": "lively",
}

# One answer, one explanation, one warning: the three things persona v2 does most.
TEXT = (
    "De printer is klaar, de print ziet er goed uit. "
    "Een stappenmotor draait in vaste stappen van 1,8 graad; de driver bepaalt de volgorde. "
    "Let op: de accu staat op elf volt, ik zou hem binnen het uur laden."
)
STYLE = "Spreek dit kalm, beschaafd en zeker uit, in een rustig maar vlot tempo, zoals een bekwame butler:"

OUT = REPO / ".local" / "voices"


def main() -> int:
    load_env(REPO / ".env")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY missing from .env", file=sys.stderr)
        return 2
    voices = sys.argv[1:] or list(CANDIDATES)
    order = list(CANDIDATES) + [v for v in voices if v not in CANDIDATES]
    OUT.mkdir(parents=True, exist_ok=True)
    for voice in voices:
        started = time.monotonic()
        pcm = None
        # The TTS preview has a small per-minute quota (429 after ~4 samples), so wait it out.
        for attempt in range(4):
            try:
                # Retries go to the older TTS model: it has its own quota.
                model = speech.TTS_MODEL if attempt == 0 else speech.TTS_FALLBACK_MODEL
                pcm = speech.gemini_pcm(TEXT, key, model=model, voice=voice, style=STYLE, timeout=60)
                break
            except RuntimeError as exc:
                if "429" not in str(exc) or attempt == 3:
                    print(f"{voice:12} FAILED {str(exc)[:120]}", file=sys.stderr)
                    break
                if attempt:
                    print(f"{voice:12} quota, waiting 30 s", flush=True)
                    time.sleep(30)
        if pcm is None:
            continue
        for old in OUT.glob(f"*_{voice}.wav"):
            old.unlink()
        path = OUT / f"{order.index(voice) + 1}_{voice}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(speech.TTS_RATE)
            w.writeframes(pcm)
        print(f"{voice:12} {len(pcm) / 2 / speech.TTS_RATE:4.1f} s audio, {time.monotonic() - started:4.1f} s to make"
              f"  - {CANDIDATES.get(voice, '')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
