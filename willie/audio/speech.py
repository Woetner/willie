"""Text-to-speech for WILL-E, out through the MAX98357A I2S amp (B6).

Two backends, tried in this order:

  gemini   Gemini's TTS model returns 24 kHz PCM. It is the only one whose
           Dutch is properly intelligible, and the console is already talking
           to Gemini, so it costs no new dependency - only network and latency.
  espeak   espeak-ng, offline and instant, but its Dutch is hard to follow.
           Kept as the fallback for when the network is down.

Neither is the robot's real voice: that is the Gate G1 adapter (D4/D5), which
streams audio inside a live session instead of asking for a whole sentence.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request

# The amp is the only card once dtparam=audio=off is set, but name it anyway so
# a plugged-in USB sound card cannot steal the speech. plughw lets ALSA resample
# whatever rate the backend produces to what the I2S block wants.
DEVICE = "plughw:CARD=MAX98357A,DEV=0"

# --- Gemini backend ------------------------------------------------------
# Listed on the key 20 Sep; the 2.5 preview is the documented older name.
TTS_MODEL = os.environ.get("WILLIE_TTS_MODEL", "gemini-3.1-flash-tts-preview")
TTS_FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"
# Prebuilt voices are multilingual. Puck is bright and young, which suits him.
TTS_VOICE = os.environ.get("WILLIE_TTS_VOICE", "Puck")
TTS_STYLE = "Spreek als een vriendelijke, nieuwsgierige werkplaatsrobot. Rustig tempo, duidelijk articuleren:"
TTS_RATE = 24_000  # what the model returns: 16-bit mono PCM

# --- espeak-ng fallback --------------------------------------------------
VOICE = "nl"
SPEED = 165
PITCH = 40
AMPLITUDE = 150


def available() -> bool:
    """True when at least the offline backend can speak."""
    return shutil.which("espeak-ng") is not None


def to_display(text: str) -> str:
    """Flatten Dutch accents (e, e, i) to plain ASCII for the 7-row screen font."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c))


def _play_pcm(pcm: bytes, rate: int, channels: int = 1) -> None:
    subprocess.run(
        ["aplay", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(rate), "-c", str(channels), "-t", "raw"],
        input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def gemini_pcm(text: str, api_key: str, model: str = TTS_MODEL, timeout: float = 30.0) -> bytes:
    """Ask Gemini to read `text`. Returns raw 16-bit PCM, or raises RuntimeError."""
    body = json.dumps({
        "contents": [{"parts": [{"text": f"{TTS_STYLE} {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": TTS_VOICE}}},
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"TTS HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"TTS unreachable: {exc}") from exc
    try:
        for part in payload["candidates"][0]["content"]["parts"]:
            data = part.get("inlineData") or part.get("inline_data")
            if data and data.get("data"):
                return base64.b64decode(data["data"])
    except (IndexError, KeyError, TypeError):
        pass
    raise RuntimeError("TTS returned no audio: " + json.dumps(payload)[:200])


def espeak(text: str, voice: str = VOICE) -> None:
    if not shutil.which("espeak-ng"):
        return
    speech = subprocess.Popen(
        ["espeak-ng", "-v", voice, "-s", str(SPEED), "-p", str(PITCH), "-a", str(AMPLITUDE), "--stdout", text],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    player = subprocess.Popen(
        ["aplay", "-q", "-D", DEVICE], stdin=speech.stdout,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if speech.stdout:
        speech.stdout.close()
    player.wait()
    speech.wait()


def speak(text: str, voice: str = VOICE, api_key: str | None = None) -> str:
    """Say `text` out loud. Returns the backend that spoke ('gemini'/'espeak'/'').

    Never raises: a mute robot is better than a crashed one, and the answer is
    on the screen either way.
    """
    text = text.strip()
    if not text:
        return ""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if key:
        for model in (TTS_MODEL, TTS_FALLBACK_MODEL):
            try:
                started = time.monotonic()
                pcm = gemini_pcm(text, key, model)
                latency = time.monotonic() - started
                os.environ["WILLIE_TTS_LAST_MS"] = f"{latency * 1000:.0f}"
                _play_pcm(pcm, TTS_RATE)
                return "gemini"
            except RuntimeError:
                continue
    espeak(text, voice)
    return "espeak" if shutil.which("espeak-ng") else ""
