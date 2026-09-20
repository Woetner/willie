"""Offline text-to-speech for WILL-E, through the MAX98357A I2S amp (B6).

espeak-ng is deliberate: it is ~4 MB on disk, holds no model in RAM between
calls and starts speaking immediately, which matters on a 512 MB Pi 3 A+ (D20,
D21). Its robotic timbre is also close to the voice D7 asks for, so the filter
there can stay light. A neural voice (Piper) would sound better but costs a
~60 MB model plus onnxruntime, so it waits for the Gate G2 decision.

The real conversational voice is the Gate G1 adapter (D4/D5); this module is
what the bench tools and the face console speak with until then.
"""

from __future__ import annotations

import shutil
import subprocess
import unicodedata

# The amp is the only card once dtparam=audio=off is set, but name it anyway so
# a plugged-in USB sound card cannot steal the speech. plughw lets ALSA resample
# espeak-ng's 22.05 kHz to whatever the I2S block wants.
DEVICE = "plughw:CARD=MAX98357A,DEV=0"
VOICE = "nl"          # Dutch
SPEED = 165           # words per minute; 175 is espeak-ng's default
PITCH = 40            # 0-99, lower = more machine, less chirp
AMPLITUDE = 150       # 0-200. The amp has no volume control of its own (B6).


def available() -> bool:
    return shutil.which("espeak-ng") is not None


def speak(text: str, voice: str = VOICE, blocking: bool = True) -> subprocess.Popen | None:
    """Say `text` out loud. Returns the aplay process when blocking is False.

    Never raises: a mute robot is better than a crashed one, and the answer is
    on the screen either way.
    """
    text = text.strip()
    if not text or not available():
        return None
    speech = subprocess.Popen(
        ["espeak-ng", "-v", voice, "-s", str(SPEED), "-p", str(PITCH), "-a", str(AMPLITUDE), "--stdout", text],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    player = subprocess.Popen(
        ["aplay", "-q", "-D", DEVICE],
        stdin=speech.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if speech.stdout:
        speech.stdout.close()  # so espeak-ng sees the pipe close when aplay stops
    if blocking:
        player.wait()
        speech.wait()
        return None
    return player


def to_display(text: str) -> str:
    """Flatten Dutch accents (ë, é, ï) to plain ASCII for the 7-row screen font."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c))
