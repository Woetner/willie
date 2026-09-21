#!/usr/bin/env python3
"""Capture one camera image and ask Gemini a question about it.

Run on the Pi:
    .venv/bin/python tools/ask_camera.py
    .venv/bin/python tools/ask_camera.py --question "What is in front of you?"

The image stays local unless --save is supplied.  This is intentionally an
on-demand bench tool: it starts no process and adds no dependency to WILL-E.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Gemini returned a 404 for 2.5 Flash Lite on 2026-09-20 and directs new users
# to this replacement model.
MODEL = "gemini-3.5-flash-lite"
API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL}:generateContent"
)


def load_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without printing sensitive values."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Against blurry and shaky photos (21 Sep): a short burst instead of one shot.
# - continuous autofocus over the full range (incl. workshop close-ups), running while
#   the burst is taken, so later frames are in focus even if the first is not
# - "sport" exposure: shorter shutter times (more gain) = less motion blur from a hand
#   holding a part up, or from the pan-tilt still settling
# - keep the sharpest frame: variance of the Laplacian on a small grayscale copy
# Measured on the Pi: 5 frames in 3.0 s, scoring all five 0.06 s.
BURST_MS, BURST_EVERY_MS = 2600, 300


def sharpness(jpeg: Path) -> float:
    """Higher = sharper. Laplacian variance at 1/4 size; JPEG size if numpy is missing."""
    try:
        import numpy as np
    except ImportError:
        return float(jpeg.stat().st_size)
    out = subprocess.run(["djpeg", "-grayscale", "-scale", "1/4", "-pnm", str(jpeg)],
                         capture_output=True, check=False).stdout
    parts = out.split(b"\n", 3)
    if len(parts) < 4 or not parts[0].startswith(b"P5"):
        return float(jpeg.stat().st_size)
    width, height = map(int, parts[1].split())
    g = np.frombuffer(parts[3], np.uint8)[: width * height].reshape(height, width).astype(np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def capture(path: Path, quiet: bool = False) -> None:
    if not shutil.which("rpicam-still"):
        raise RuntimeError("rpicam-still is not installed; run the camera setup first.")
    with tempfile.TemporaryDirectory(prefix="willie-burst-") as tmp:
        command = [
            "rpicam-still", "--nopreview", "--zsl",
            "--autofocus-mode", "continuous", "--autofocus-range", "full",
            "--exposure", "sport", "--denoise", "cdn_hq",
            "--width", "1024", "--height", "768", "--encoding", "jpg", "--quality", "90",
            "--timeout", str(BURST_MS), "--timelapse", str(BURST_EVERY_MS),
            "--output", str(Path(tmp) / "frame_%02d.jpg"),
        ]
        subprocess.run(command, check=False,
                       stdout=subprocess.DEVNULL if quiet else None,
                       stderr=subprocess.DEVNULL if quiet else None)
        frames = [f for f in Path(tmp).glob("frame_*.jpg") if f.stat().st_size > 0]
        if frames:
            shutil.copy(max(frames, key=sharpness), path)
            return
    _capture_single(path, quiet)       # burst failed (old rpicam?): one autofocus shot


def _capture_single(path: Path, quiet: bool = False) -> None:
    command = [
        "rpicam-still",
        "--nopreview",
        # The Camera Module 3 has autofocus. Without these it shoots at whatever
        # the lens happened to be at, which is blurry for workshop close-ups.
        "--autofocus-on-capture",
        "--timeout",
        "2000",
        "--width",
        "1024",
        "--height",
        "768",
        "--encoding",
        "jpg",
        "--quality",
        "85",
        "--output",
        str(path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Camera capture failed. Check its flex cable and rpicam-hello.") from exc
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("Camera capture did not produce an image.")


# WILL-E is spoken to in Dutch, and espeak-ng reads Dutch with the Dutch voice,
# so the model answers in Dutch unless a caller asks for something else.
LANGUAGE_RULE = {
    "nl": "Antwoord altijd in het Nederlands, in hooguit twee korte zinnen.",
    "en": "Always answer in English, in at most two short sentences.",
}

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from willie.voice.persona import system_prompt  # noqa: E402


def ask_gemini(image: Path, question: str, api_key: str, language: str = "nl") -> str:
    image_b64 = base64.b64encode(image.read_bytes()).decode("ascii")
    body = json.dumps(
        {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                system_prompt()
                                + "\n\nJe kijkt nu door je camera. Zeg het als het beeld onduidelijk is. "
                                + LANGUAGE_RULE.get(language, LANGUAGE_RULE["nl"])
                                + " Your answer is read aloud by a speech synthesiser, so write plain "
                                "sentences: no lists, no markdown, no emoji.\n\nUser question: " + question
                            )
                        },
                        {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                    ]
                }
            ],
            "generationConfig": {"maxOutputTokens": 200, "temperature": 0.2},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Gemini: {exc.reason}") from exc

    try:
        return "".join(part["text"] for part in payload["candidates"][0]["content"]["parts"] if "text" in part).strip()
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Gemini returned no text response: " + json.dumps(payload)) from exc


def ask_gemini_audio(wav: bytes, api_key: str, language: str = "nl") -> str:
    """Send spoken audio as the whole prompt - no picture, no typing."""
    body = json.dumps(
        {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                system_prompt()
                                + "\n\nDe audio is Wouter die tegen je praat. Antwoord op wat hij zegt. "
                                + LANGUAGE_RULE.get(language, LANGUAGE_RULE["nl"])
                                + " Your answer is read aloud by a speech synthesiser, so write plain "
                                "sentences: no lists, no markdown, no emoji. If the audio is unclear, "
                                "say so and ask them to repeat it."
                            )
                        },
                        {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode("ascii")}},
                    ]
                }
            ],
            "generationConfig": {"maxOutputTokens": 200, "temperature": 0.4},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Gemini: {exc.reason}") from exc
    try:
        return "".join(part["text"] for part in payload["candidates"][0]["content"]["parts"] if "text" in part).strip()
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Gemini returned no text response: " + json.dumps(payload)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Take one picture and ask Gemini about it.")
    parser.add_argument("--question", help="Question to ask. Prompts at the keyboard when omitted.")
    parser.add_argument("--save", metavar="PATH", help="Keep the captured JPEG at PATH.")
    parser.add_argument("--language", default="nl", choices=sorted(LANGUAGE_RULE), help="Answer language (default: nl).")
    parser.add_argument("--speak", action="store_true", help="Also say the answer through the speaker (B6).")
    args = parser.parse_args()

    load_env(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is missing from ~/willie/.env", file=sys.stderr)
        return 2

    question = args.question or input("What do you want me to look at? ").strip()
    if not question:
        print("No question supplied.", file=sys.stderr)
        return 2

    saved_path = Path(args.save).expanduser() if args.save else None
    with tempfile.TemporaryDirectory(prefix="willie-camera-") as temp_dir:
        image = saved_path or Path(temp_dir) / "view.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        print("Capturing image…")
        capture(image)
        print("Asking Gemini…")
        answer = ask_gemini(image, question, api_key, args.language)
    print("\nWILL-E:", answer)
    if args.speak:
        from willie.audio import speech

        if not speech.available():
            print("(espeak-ng is not installed, so nothing was spoken)", file=sys.stderr)
        speech.speak(answer, voice=args.language)
    if saved_path:
        print(f"Saved image: {saved_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
