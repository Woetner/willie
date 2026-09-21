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

import array
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
# The googlevoicehat overlay (B7: amp + mic on one card) names the card this.
DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"

# --- Gemini backend ------------------------------------------------------
# Listed on the key 20 Sep; the 2.5 preview is the documented older name.
TTS_MODEL = os.environ.get("WILLIE_TTS_MODEL", "gemini-3.1-flash-tts-preview")
TTS_FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"
# Prebuilt voices are multilingual. Puck is bright and young, which suits him.
TTS_VOICE = os.environ.get("WILLIE_TTS_VOICE", "Puck")
# Matches config/persona.md: direct, quick, no announcer warmth.
TTS_STYLE = "Zeg dit vlot en zakelijk, in een stevig tempo, zonder opgewekte ondertoon:"
TTS_RATE = 24_000  # what the model returns: 16-bit mono PCM

# --- espeak-ng fallback --------------------------------------------------
VOICE = "nl"
SPEED = 185
PITCH = 40
AMPLITUDE = 150


def available() -> bool:
    """True when at least the offline backend can speak."""
    return shutil.which("espeak-ng") is not None


def to_display(text: str) -> str:
    """Flatten Dutch accents (e, e, i) to plain ASCII for the 7-row screen font."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c))


# Measured on the bench 20 Sep: this card plays 48 kHz fine and is silent at
# 16 kHz, whatever plughw claims to convert. So everything is upsampled to
# 48 kHz here instead of being handed to ALSA at its native rate.
CARD_RATE = 48_000

# The MAX98357A has no mixer control of its own (checked with amixer on 20 Sep),
# so volume is done here by scaling samples. 0.0 is silence, 1.0 is as loud as
# the amp goes, which is far too loud on a desk.
VOLUME = float(os.environ.get("WILLIE_VOLUME", "0.15"))


def scale(pcm: bytes, gain: float = None) -> bytes:
    """Apply the software volume. Returns the PCM unchanged at gain 1.0."""
    gain = VOLUME if gain is None else gain
    if gain >= 0.999:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    for i, value in enumerate(samples):
        samples[i] = int(value * gain)
    return samples.tobytes()


def _upsample(pcm: bytes, rate: int) -> bytes:
    """Repeat each sample to reach CARD_RATE. Rates that do not divide evenly
    are rounded up, which shifts the pitch slightly but keeps speech clear."""
    if rate >= CARD_RATE:
        return pcm
    factor = -(-CARD_RATE // rate)
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    out = array.array("h")
    for value in samples:
        for _ in range(factor):
            out.append(value)
    return out.tobytes()


def _play_pcm(pcm: bytes, rate: int, channels: int = 1) -> None:
    pcm = scale(pcm)
    if channels == 1 and rate < CARD_RATE:
        pcm, rate = _upsample(pcm, rate), CARD_RATE
    subprocess.run(
        ["aplay", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(rate), "-c", str(channels), "-t", "raw"],
        input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def gemini_pcm(text: str, api_key: str, model: str = TTS_MODEL, timeout: float = 30.0,
               voice: str = TTS_VOICE, style: str = TTS_STYLE) -> bytes:
    """Ask Gemini to read `text`. Returns raw 16-bit PCM, or raises RuntimeError."""
    body = json.dumps({
        "contents": [{"parts": [{"text": f"{style} {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
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
    """Offline fallback. espeak-ng speaks at 22.05 kHz, which this card will not
    play, so the WAV header is stripped and the samples go through _play_pcm."""
    if not shutil.which("espeak-ng"):
        return
    result = subprocess.run(
        ["espeak-ng", "-v", voice, "-s", str(SPEED), "-p", str(PITCH), "-a", str(AMPLITUDE), "--stdout", text],
        capture_output=True, check=False,
    )
    wav = result.stdout
    if len(wav) < 44:
        return
    _play_pcm(wav[44:], 22_050)


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
