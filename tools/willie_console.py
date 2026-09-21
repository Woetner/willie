#!/usr/bin/env python3
"""A fullscreen voice console for WILL-E's face.

Runs on demand on the Pi. It writes the face directly to every available Linux
framebuffer, listens for "Hey Willie", and answers through Gemini. Camera use is
automatic: ordinary questions stay audio-only, while "look at this" captures a
still. Pass --keyboard to retain the earlier typed camera-question mode.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import termios
from contextlib import contextmanager
from pathlib import Path

# The short /usr/local/bin/willie launcher executes this script by absolute path,
# so the repository root is not otherwise guaranteed to be on sys.path.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ask_camera import ask_gemini, capture, load_env
from ask_voice import answer as answer_voice
from ask_voice import understand
from willie.audio import record, speech
from willie.face.runtime import Face
from willie.face.renderer import CYAN, AMBER


@contextmanager
def quiet_keyboard():
    """Read individual keys without echoing them onto the console framebuffer."""
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        sys.stdout.write("\033[2J\033[H\033[?25l")
        sys.stdout.flush()
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()


def keyboard_loop(face: Face, api_key: str) -> int:
    question = ""
    showed_answer = False
    with quiet_keyboard() as interactive:
        if not interactive:
            raise RuntimeError("Run keyboard mode from the Pi's attached keyboard, not a pipe.")
        face.idle()
        while True:
            char = os.read(sys.stdin.fileno(), 1)
            if char == b"\x03":  # Ctrl-C
                return 0
            if char in (b"\r", b"\n"):
                if not question:
                    continue
                try:
                    face.status("SEEING", CYAN)
                    with tempfile.TemporaryDirectory(prefix="willie-camera-") as temp_dir:
                        image = Path(temp_dir) / "view.jpg"
                        capture(image, quiet=True)
                        face.status("THINKING", AMBER)
                        answer = ask_gemini(image, question, api_key)
                    face.answer(answer, talking=True)
                    speech.speak(answer, api_key=api_key)
                    face.answer(answer)
                    question = ""
                    showed_answer = True
                except RuntimeError as exc:
                    face.error(str(exc))
                    speech.speak("Sorry, dat ging mis.", api_key=api_key)
                    question = ""
                    showed_answer = True
                continue
            if char in (b"\x7f", b"\x08"):
                question = question[:-1]
            elif 32 <= char[0] <= 126:
                if showed_answer:
                    question, showed_answer = "", False
                if len(question) < 180:
                    question += char.decode("ascii")
            if not showed_answer:
                face.idle(question)


def _listen(face: Face, label: str, *, wait_for_speech: float) -> bytes:
    face.status(label, CYAN)
    audio, _seconds = record.listen(
        max_seconds=max(15.0, wait_for_speech),
        lead_in_seconds=wait_for_speech,
    )
    return audio


def voice_loop(face: Face, api_key: str) -> int:
    """Cloud-routed bench wake loop; the production wake detector is D2, local on the Pi."""
    while True:
        try:
            face.waiting()
            audio = _listen(face, "LISTENING", wait_for_speech=12.0)
            if not audio:
                continue
            face.status("HEARING", AMBER)
            decision = understand(audio, api_key, wake_required=True)
            if not decision.wake_detected:
                continue
            if not decision.has_question:
                face.status("YES?", CYAN)
                speech.espeak("Ja?")
                audio = _listen(face, "LISTENING", wait_for_speech=8.0)
                if not audio:
                    continue
                face.status("THINKING", AMBER)
                decision = understand(audio, api_key, wake_required=False)
            if decision.needs_camera:
                face.status("SEEING", CYAN)
            else:
                face.status("THINKING", AMBER)
            response, _used_camera = answer_voice(audio, api_key, decision=decision)
            face.answer(response, talking=True)
            speech.speak(response, api_key=api_key)
            face.answer(response)
        except KeyboardInterrupt:
            return 0
        except RuntimeError as exc:
            face.error(str(exc))
            speech.espeak("Sorry, dat ging mis.")


def main() -> int:
    parser = argparse.ArgumentParser(description="WILL-E face and voice console.")
    parser.add_argument("--keyboard", action="store_true", help="Use the old typed camera-question mode.")
    args = parser.parse_args()

    load_env(REPO / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is missing from ~/willie/.env", file=sys.stderr)
        return 2
    face = Face.open()
    try:
        return keyboard_loop(face, api_key) if args.keyboard else voice_loop(face, api_key)
    finally:
        face.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
