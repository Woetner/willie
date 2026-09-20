#!/usr/bin/env python3
"""Bench prototype: hold a spoken conversation with Gemini Live (D4 probe).

Run on the Pi:  .venv/bin/python tools/live_talk.py [seconds]
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
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
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
        elif kind == "uplink":
            print(f"[{elapsed:5.1f}s] mic -> {detail}")
        elif kind == "error":
            print(f"[{elapsed:5.1f}s] ERROR {detail}")
        elif kind == "turn_complete":
            print(f"[{elapsed:5.1f}s] turn complete")

    try:
        model = asyncio.run(gemini_live.session(key, seconds=seconds, on_event=on_event))
    except RuntimeError as exc:
        print(f"Live session failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(f"done, model {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
