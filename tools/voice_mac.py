#!/usr/bin/env python3
"""Talk to WILL-E with the MacBook's microphone and speakers.

This is a temporary bench bridge, not part of the robot: the Mac only records
and speaks, while the Pi keeps the camera and the API key. Nothing about it
changes the plan in WILL-E.md - the real voice path is the on-robot realtime
adapter chosen at Gate G1 (D9).

    make voice

Press Enter to start recording, Enter again to stop. The recording is piped over
SSH to tools/ask_voice.py on the Pi, which captures one still and asks Gemini.
The answer is printed and read aloud with the built-in `say` command.

Requires `sounddevice` on the Mac (its wheel bundles PortAudio, so no Homebrew).
The first run makes macOS ask for microphone permission for your terminal.
"""

from __future__ import annotations

import argparse
import io
import queue
import subprocess
import sys
import threading
import wave

SAMPLE_RATE = 16000
CHANNELS = 1


def record_until_enter() -> bytes:
    """Record 16 kHz mono PCM until the user presses Enter. Returns a WAV file."""
    import sounddevice

    frames: queue.Queue[bytes] = queue.Queue()
    stop = threading.Event()

    def callback(indata, _frames, _time, status):
        if status:
            print(f"  (audio: {status})", file=sys.stderr)
        frames.put(bytes(indata))

    with sounddevice.RawInputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=callback
    ):
        print("Recording… press Enter to stop.", file=sys.stderr)
        input()
        stop.set()

    chunks = []
    while not frames.empty():
        chunks.append(frames.get())

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(chunks))
    return buffer.getvalue()


def ask_pi(host: str, pi_dir: str, audio: bytes, no_camera: bool) -> str:
    command = f"cd {pi_dir} && .venv/bin/python tools/ask_voice.py"
    if no_camera:
        command += " --no-camera"
    result = subprocess.run(
        ["ssh", host, command], input=audio, stdout=subprocess.PIPE, stderr=None, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"the Pi returned exit code {result.returncode}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def speak(text: str, voice: str | None) -> None:
    command = ["say"]
    if voice:
        command += ["-v", voice]
    subprocess.run(command + [text], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice bridge: Mac mic/speaker, Pi camera and AI.")
    parser.add_argument("--host", default="willie.local")
    parser.add_argument("--pi-dir", default="willie")
    parser.add_argument("--voice", default=None, help="A `say` voice name, e.g. Daniel.")
    parser.add_argument("--no-camera", action="store_true", help="Answer from the audio alone.")
    parser.add_argument("--once", action="store_true", help="Ask one question and exit.")
    args = parser.parse_args()

    try:
        import sounddevice  # noqa: F401
    except (ImportError, OSError) as exc:
        print(f"sounddevice is not available: {exc}", file=sys.stderr)
        print("Install it with:  .venv/bin/pip install sounddevice", file=sys.stderr)
        return 2

    print("WILL-E voice bridge. Ctrl+C to quit.", file=sys.stderr)
    while True:
        try:
            input("\nPress Enter to speak… ")
            audio = record_until_enter()
            seconds = (len(audio) - 44) / (SAMPLE_RATE * 2)
            if seconds < 0.3:
                print("That was too short to hear.", file=sys.stderr)
                continue
            print(f"Sending {seconds:.1f} s to the Pi…", file=sys.stderr)
            answer = ask_pi(args.host, args.pi_dir, audio, args.no_camera)
        except KeyboardInterrupt:
            print("\nBye.", file=sys.stderr)
            return 0
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            continue

        if not answer:
            print("No answer came back.", file=sys.stderr)
            continue
        print(f"\nWILL-E: {answer}")
        speak(answer, args.voice)
        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
