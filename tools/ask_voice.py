#!/usr/bin/env python3
"""Answer a spoken question, optionally using WILL-E's camera.

Runs on the Pi. Reads one WAV file from stdin and asks Gemini whether the
question needs vision. Ordinary questions are answered from audio alone; a
still is captured only for requests that need WILL-E to look at something.
The answer goes to stdout on its own so a caller can pipe it to speech.

    ssh willie.local 'cd willie && .venv/bin/python tools/ask_voice.py' < question.wav

Like ask_camera.py this is an on-demand bench tool: stdlib only, no resident
process, and nothing is written to disk unless --save is supplied.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
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

VOICE_ROUTER_PROMPT = """
You are the ears and short-answer brain of WILL-E, a small curious workshop robot.
Listen to the attached WAV. Return one JSON object and nothing else, with exactly:
{"wake_detected": boolean, "has_question": boolean, "needs_camera": boolean, "answer": string}

Rules:
- The wake phrase is "Hey Willie". Accept natural Dutch or English pronunciation of Willie.
- wake_detected is true only when the recording starts with that wake phrase. If wake_required
  below is false, set wake_detected true.
- has_question is true when the user says a real request besides the wake phrase.
- needs_camera is true only when answering requires seeing the current physical scene, for
  example "look at this", "what is this", or checking a visible object. General knowledge,
  conversation, calculations, and advice do not need the camera.
- If there is no wake when one is required, or no question, answer must be empty.
- If needs_camera is true, answer must be empty; another request will include the photo.
- Otherwise answer the question in the language used by the user, in at most two short spoken
  sentences, with no markdown, lists, or emoji. Admit uncertainty.

wake_required: {wake_required}
""".strip()


@dataclass(frozen=True)
class VoiceDecision:
    wake_detected: bool
    has_question: bool
    needs_camera: bool
    answer: str


def _gemini(audio: bytes, prompt: str, api_key: str, *, json_response: bool = False) -> dict:
    generation_config: dict = {"maxOutputTokens": 200, "temperature": 0.2}
    if json_response:
        generation_config["responseMimeType"] = "application/json"
    body = json.dumps(
        {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": base64.b64encode(audio).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": generation_config,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Gemini: {exc.reason}") from exc


def _response_text(payload: dict) -> str:
    try:
        return "".join(
            part["text"] for part in payload["candidates"][0]["content"]["parts"] if "text" in part
        ).strip()
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Gemini returned no text response: " + json.dumps(payload)) from exc


def understand(audio: bytes, api_key: str, *, wake_required: bool) -> VoiceDecision:
    """Detect the wake phrase, camera need, and (when possible) answer the audio."""
    prompt = VOICE_ROUTER_PROMPT.replace("{wake_required}", str(wake_required).lower())
    raw = _response_text(_gemini(audio, prompt, api_key, json_response=True))
    try:
        value = json.loads(raw)
        return VoiceDecision(
            wake_detected=value["wake_detected"] is True,
            has_question=value["has_question"] is True,
            needs_camera=value["needs_camera"] is True,
            answer=str(value.get("answer", "")).strip(),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Gemini returned an invalid voice decision: {raw[:300]}") from exc


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
    return _response_text(payload)


def answer(
    audio: bytes,
    api_key: str,
    *,
    camera_mode: str = "auto",
    decision: VoiceDecision | None = None,
    save: Path | None = None,
) -> tuple[str, bool]:
    """Answer audio and return ``(text, used_camera)``.

    ``camera_mode`` is auto, always, or never. Auto reuses the router answer
    when vision is unnecessary, avoiding both a photo and a second API call.
    """
    if camera_mode not in {"auto", "always", "never"}:
        raise ValueError(f"unknown camera mode: {camera_mode}")
    current = decision or understand(audio, api_key, wake_required=False)
    use_camera = camera_mode == "always" or (camera_mode == "auto" and current.needs_camera)
    if not use_camera:
        if current.answer:
            return current.answer, False
        return ask(audio, None, api_key), False

    with tempfile.TemporaryDirectory(prefix="willie-voice-") as temp_dir:
        image = save or Path(temp_dir) / "view.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        print("Capturing image…", file=sys.stderr)
        capture(image, quiet=True)
        return ask(audio, image, api_key), True


def main() -> int:
    parser = argparse.ArgumentParser(description="Answer a spoken question, using the camera only when needed.")
    camera = parser.add_mutually_exclusive_group()
    camera.add_argument("--camera", action="store_true", help="Always capture a photo.")
    camera.add_argument("--no-camera", action="store_true", help="Never capture a photo.")
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
    mode = "always" if args.camera else "never" if args.no_camera else "auto"
    print("Asking Gemini…", file=sys.stderr)
    answer_text, _used_camera = answer(audio, api_key, camera_mode=mode, save=saved)

    print(answer_text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
