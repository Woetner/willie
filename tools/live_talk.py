#!/usr/bin/env python3
"""Bench prototype: hold a spoken conversation with Gemini Live (D4 probe).

Run on the Pi:  .venv/bin/python tools/live_talk.py [seconds]   (no seconds = until Ctrl-C)
Ctrl-C stops it. Nothing is left running afterwards.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from ask_camera import load_env
from willie.voice import gemini_live


def main() -> int:
    load_env(REPO / ".env")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY missing from .env", file=sys.stderr)
        return 2
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else None   # None: until Ctrl-C
    started = time.monotonic()
    first_audio: list[float] = []

    def on_event(kind: str, detail: str) -> None:
        elapsed = time.monotonic() - started
        if kind == "ready":
            print(f"[{elapsed:5.1f}s] connected: {detail} - talk to him, Dutch is fine")
        elif kind == "audio":
            if not first_audio:
                first_audio.append(elapsed)
                print(f"[{elapsed:5.1f}s] first audio back")
        elif kind == "text":
            print(f"[{elapsed:5.1f}s] text: {detail}")
        elif kind == "interrupted":
            print(f"[{elapsed:5.1f}s] interrupted (barge-in)")
        elif kind == "tool":
            print(f"[{elapsed:5.1f}s] tool {detail}")
        elif kind == "tool_result":
            print(f"[{elapsed:5.1f}s]      {detail}")
        elif kind == "uplink":
            print(f"[{elapsed:5.1f}s] mic -> {detail}")
        elif kind == "error":
            print(f"[{elapsed:5.1f}s] ERROR {detail}")
        elif kind == "turn_complete":
            print(f"[{elapsed:5.1f}s] turn complete")

    # Without a time limit this runs until Ctrl-C. Google closes a Live connection by itself
    # after roughly 10-15 min; then it reconnects (the new session starts without the old
    # conversation, but with the memory notes).
    while True:
        try:
            model = asyncio.run(gemini_live.session(key, seconds=seconds, on_event=on_event))
        except RuntimeError as exc:
            print(f"Live session failed: {exc}", file=sys.stderr)
            if seconds is not None:
                return 1
            time.sleep(3)
            continue
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        if seconds is not None:
            print(f"done, model {model}")
            return 0
        print(f"[{time.monotonic() - started:5.1f}s] connection closed by the server - reconnecting")


if __name__ == "__main__":
    raise SystemExit(main())
