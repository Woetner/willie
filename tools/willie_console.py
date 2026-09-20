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
from willie.face.framebuffer import Framebuffer, open_all


BLACK = (0, 0, 0)
WHITE = (235, 242, 245)
CYAN = (57, 208, 255)
DIM = (53, 105, 125)
AMBER = (242, 183, 5)
RED = (255, 88, 88)

# A deliberately tiny uppercase font: enough for prompts and answers on 320px.
FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10011", "10101", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00110", "00100"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "'": ("00100", "00100", "00000", "00000", "00000", "00000", "00000"),
    " ": ("00000",) * 7,
}


class Face:
    def __init__(self, displays: list[Framebuffer]):
        self.displays = displays

    def _each(self, method: str, *args) -> None:
        for display in self.displays:
            getattr(display, method)(*args)

    def text(self, text: str, x: int, y: int, scale: int, colour=WHITE, max_width: int | None = None) -> None:
        for display in self.displays:
            cursor_x, cursor_y = x, y
            limit = max_width or display.width - x
            for char in speech.to_display(text).upper():
                glyph = FONT.get(char, FONT["?"])
                char_width = 6 * scale
                if cursor_x + char_width > x + limit:
                    cursor_x, cursor_y = x, cursor_y + 9 * scale
                for row, bits in enumerate(glyph):
                    for col, bit in enumerate(bits):
                        if bit == "1":
                            display.rect(cursor_x + col * scale, cursor_y + row * scale, scale, scale, colour)
                cursor_x += char_width

    def base(self, eye_colour=CYAN, eyelids: float = 0.0) -> None:
        for display in self.displays:
            display.fill(BLACK)
            w, h = display.width, display.height
            for cx in (w * 3 // 10, w * 7 // 10):
                display.ellipse(cx, h * 40 // 100, w // 7, h * 23 // 100, DIM)
                display.ellipse(cx, h * 40 // 100, w * 8 // 50, h * 21 // 100, eye_colour)
                display.ellipse(cx - w // 25, h * 35 // 100, w // 40, h // 35, WHITE)
                if eyelids:
                    display.rect(cx - w // 5, h * 17 // 100, w * 2 // 5, int(h * 23 // 100 * eyelids), BLACK)
            self.text("WILL-E", 8, 8, 2, AMBER)

    def idle(self, question: str = "") -> None:
        self.base()
        self.text("TYPE A QUESTION", 12, self.displays[0].height * 68 // 100, 2, WHITE)
        self.text(question[-68:] or "ENTER TO LOOK", 12, self.displays[0].height * 80 // 100, 2, CYAN)
        self.text("ENTER = CAMERA  CTRL-C = QUIT", 12, self.displays[0].height * 92 // 100, 1, DIM)

    def waiting(self) -> None:
        self.base()
        self.text("SAY HEY WILLIE", 12, self.displays[0].height * 72 // 100, 3, CYAN)
        self.text("CTRL-C = QUIT", 12, self.displays[0].height * 92 // 100, 1, DIM)

    def status(self, label: str, colour=AMBER) -> None:
        self.base(eye_colour=colour)
        self.text(label, 12, self.displays[0].height * 72 // 100, 3, colour)

    def answer(self, answer: str, talking: bool = False) -> None:
        self.base(eye_colour=AMBER if talking else CYAN)
        # Five 2x-font lines fit between the eyes and the next-question hint on 320px.
        self.text(answer[:180], 10, self.displays[0].height * 62 // 100, 2, WHITE, self.displays[0].width - 20)
        hint = "TALKING..." if talking else "TYPE AGAIN FOR NEXT QUESTION"
        self.text(hint, 10, self.displays[0].height * 91 // 100, 1, AMBER if talking else DIM)

    def error(self, message: str) -> None:
        self.base(eye_colour=RED, eyelids=0.55)
        self.text("OOPS", 12, self.displays[0].height * 62 // 100, 3, RED)
        self.text(message[:190], 12, self.displays[0].height * 74 // 100, 1, WHITE, self.displays[0].width - 24)

    def close(self) -> None:
        for display in self.displays:
            display.close()


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
    """Cloud-routed bench wake loop; the production wake detector is B13 on the MCU."""
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
    displays = open_all()
    face = Face(displays)
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
