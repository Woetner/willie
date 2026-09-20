"""Wake-word listening for WILL-E: "hey gemini", spotted with Vosk.

Picovoice/Porcupine was the first choice for its size, but its free tier is
company-only now, so this uses Vosk: offline, free, no account, no training.

The trick that makes a 66 MB recogniser usable as a wake word is the grammar.
Vosk is given a closed vocabulary of just the wake phrases plus [unk], so it is
not transcribing Dutch at large - it only decides whether what it heard was one
of those phrases. That cuts both CPU and false positives.

Cost measured on the Pi 3 A+ (D21 budget): see ~13 in WILL-E.md. If it turns
out too heavy to keep listening all day, the answer is D19 - the wake word
belongs on the MCU, and this module is the fallback D2 describes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)  # the Kaldi banner is not useful on a console showing a face

DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
RATE = 16_000
CHUNK = 4000  # 0.25 s: small enough to react, big enough not to spin the CPU

MODEL_DIR = Path(os.environ.get(
    "WILLIE_VOSK_MODEL",
    Path(__file__).resolve().parents[2] / ".local" / "models" / "vosk-model-small-nl-0.22",
))

# Dutch speech recognition writes an English name several ways, and a wake word
# that only fires on the perfect spelling is a wake word that never fires.
PHRASES = ("hey gemini", "hé gemini", "hei gemini", "hey jemini", "hey gemeni", "hey gemini")
GRAMMAR = json.dumps(sorted({*PHRASES, "[unk]"}), ensure_ascii=False)

_model: Model | None = None


def available() -> bool:
    return MODEL_DIR.exists()


def describe() -> str:
    return '"hey gemini"' if available() else f"NO MODEL at {MODEL_DIR}"


def _recognizer() -> KaldiRecognizer:
    global _model
    if _model is None:
        _model = Model(str(MODEL_DIR))     # ~1.5 s to load, so it is kept
    recognizer = KaldiRecognizer(_model, RATE, GRAMMAR)
    recognizer.SetWords(False)
    return recognizer


def _heard_wake(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in PHRASES) or ("gemini" in text and "hey" in text)


def listen_for_wake(stop_after: float | None = None, on_tick=None) -> bool:
    """Block until "hey gemini" is heard. True on a detection, False on timeout.

    The microphone is released before returning, so the live session that
    follows can open it.
    """
    if not available():
        raise RuntimeError(f"Vosk model missing at {MODEL_DIR} - see config/wakewords/README.md")
    recognizer = _recognizer()
    recorder = subprocess.Popen(
        ["arecord", "-D", DEVICE, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw", "-q"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    elapsed = 0.0
    try:
        while True:
            chunk = recorder.stdout.read(CHUNK * 2) if recorder.stdout else b""
            if len(chunk) < CHUNK * 2:
                return False
            elapsed += CHUNK / RATE
            if recognizer.AcceptWaveform(chunk):
                if _heard_wake(json.loads(recognizer.Result()).get("text", "")):
                    return True
            else:
                # Partials fire about half a second sooner than the final result,
                # which is the difference between answering and seeming deaf.
                partial = json.loads(recognizer.PartialResult()).get("partial", "")
                if _heard_wake(partial):
                    return True
                if on_tick and partial:
                    on_tick(elapsed, partial)
            if stop_after and elapsed >= stop_after:
                return False
    finally:
        recorder.terminate()
        recorder.wait(timeout=2)
