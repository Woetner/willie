#!/usr/bin/env python3
"""Answer a spoken question about what the camera sees.

Runs on the Pi. Reads one WAV file from stdin (the recorded question), captures
one still, and sends both to Gemini in a single multimodal request. The answer
goes to stdout on its own so the caller can pipe it straight into a speech
synthesiser; progress goes to stderr.

    ssh willie.local 'cd willie && .venv/bin/python tools/ask_voice.py' < question.wav

Like ask_camera.py this is an on-demand bench tool: stdlib only, no resident
process, and nothing is written to disk unless --save is supplied.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ask_camera import API_URL, capture, load_env  # noqa: E402

import os  # noqa: E402


PROMPT = (
    "You are WILL-E, a small, curious, nerdy home robot. The audio is the user speaking "
    "to you; the image is what your camera sees right now. Answer the spoken question "
    "using the image when it is relevant. Reply in at most two short spoken sentences, "
    "no markdown, no lists, no emoji, because your answer is read aloud. If the audio is "
    "unintelligible, say only that you did not catch it."
)


def ask(audio: bytes, image: Path | None, api_key: str) -> str:
    parts: list[dict] = [
        {"text": PROMPT},
        {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode("ascii")}},
    ]
    if image is not None:
        parts.append(
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image.read_bytes()).decode("ascii")}}
        )

    body = json.dumps(
        {"contents": [{"parts": parts}], "generationConfig": {"maxOutputTokens": 200, "temperature": 0.4}}
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Gemini: {exc.reason}") from exc

    try:
        return "".join(
            part["text"] for part in payload["candidates"][0]["content"]["parts"] if "text" in part
        ).strip()
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Gemini returned no text response: " + json.dumps(payload)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Answer a spoken question about the camera view.")
    parser.add_argument("--no-camera", action="store_true", help="Answer from the audio alone.")
    parser.add_argument("--save", metavar="PATH", help="Keep the captured JPEG at PATH.")
    args = parser.parse_args()

    load_env(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is missing from ~/willie/.env", file=sys.stderr)
        return 2

    audio = sys.stdin.buffer.read()
    if len(audio) < 1024:
        print("No audio arrived on stdin.", file=sys.stderr)
        return 2

    saved = Path(args.save).expanduser() if args.save else None
    with tempfile.TemporaryDirectory(prefix="willie-voice-") as temp_dir:
        image = None
        if not args.no_camera:
            image = saved or Path(temp_dir) / "view.jpg"
            image.parent.mkdir(parents=True, exist_ok=True)
            print("Capturing image…", file=sys.stderr)
            capture(image, quiet=True)
        print("Asking Gemini…", file=sys.stderr)
        answer = ask(audio, image, api_key)

    print(answer)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
