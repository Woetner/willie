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


MODEL = "gemini-2.5-flash-lite"
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


def capture(path: Path) -> None:
    if not shutil.which("rpicam-still"):
        raise RuntimeError("rpicam-still is not installed; run the camera setup first.")
    command = [
        "rpicam-still",
        "--nopreview",
        "--timeout",
        "800",
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
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Camera capture failed. Check its flex cable and rpicam-hello.") from exc
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("Camera capture did not produce an image.")


def ask_gemini(image: Path, question: str, api_key: str) -> str:
    image_b64 = base64.b64encode(image.read_bytes()).decode("ascii")
    body = json.dumps(
        {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "You are WILL-E's visual assistant. Answer concisely and honestly. "
                                "If the image is unclear, say so.\n\nUser question: " + question
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Take one picture and ask Gemini about it.")
    parser.add_argument("--question", help="Question to ask. Prompts at the keyboard when omitted.")
    parser.add_argument("--save", metavar="PATH", help="Keep the captured JPEG at PATH.")
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
        answer = ask_gemini(image, question, api_key)
    print("\nWILL-E:", answer)
    if saved_path:
        print(f"Saved image: {saved_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
