"""Flash new ESP32 firmware over the Pi <-> MCU link, no USB (firmware 0.4.0+).

    make flash-link                         (from the Mac: build, copy, flash, check)
    .venv/bin/python tools/mcu_ota.py firmware.bin [--port /dev/serial0]    (on the Pi, core stopped)

Protocol (firmware/src/main.cpp): `ota <size> <md5>` -> `ok ota <chunk>`, then `otad <offset> <hex>`
per chunk, each answered with `ok otad <bytes written>`, then `ota!`. The ESP32 checks the MD5,
restarts into the new image and keeps it only after it hears a valid line from the Pi; otherwise
it rolls back to the old firmware after 60 s.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

import serial

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from willie.hal import proto  # noqa: E402


class Mcu:
    def __init__(self, port: str, baud: int):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.buf = b""

    def send(self, *words):
        self.ser.write(proto.encode(*words))

    def wait(self, pred, timeout: float) -> list[str] | None:
        """First valid line whose words satisfy `pred`, or None after `timeout` s."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.buf += self.ser.read(4096)
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    words = proto.decode(line)
                except proto.BadLine:
                    continue
                if words and words[0] == "err" and len(words) > 1 and words[1] == "ota":
                    raise SystemExit("MCU: " + " ".join(words))
                if words and pred(words):
                    return words
        return None


def hello(mcu: Mcu, timeout: float) -> str | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        mcu.send("hello?")
        w = mcu.wait(lambda w: w[0] == "hello", 0.5)
        if w:
            return " ".join(w[1:])
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=921600)
    a = ap.parse_args()

    data = open(a.image, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    mcu = Mcu(a.port, a.baud)

    old = hello(mcu, 3)
    if not old:
        raise SystemExit("no answer from the MCU (is the willie service stopped? is it powered?)")
    print(f"running: {old}")
    if tuple(int(x) for x in old.split()[1].split(".")[:2]) < (0, 4):
        raise SystemExit("this firmware has no link update yet: flash it once over USB (make flash)")

    mcu.send("ota", len(data), md5)
    w = mcu.wait(lambda w: w[:2] == ["ok", "ota"], 5)
    if not w:
        raise SystemExit("MCU did not accept `ota`")
    chunk = int(w[2])

    t0, done = time.monotonic(), 0
    while done < len(data):
        piece = data[done:done + chunk]
        for attempt in range(5):
            mcu.send("otad", done, piece.hex())
            w = mcu.wait(lambda w: w[:2] == ["ok", "otad"], 1.0)
            if w:
                break
        else:
            mcu.send("ota0")
            raise SystemExit(f"no answer at byte {done}; update aborted, old firmware still runs")
        done = int(w[2])                     # the MCU says where it is (handles a lost ack)
        print(f"\r{done * 100 // len(data):3d} %  {done}/{len(data)} bytes", end="", flush=True)
    print(f"\nsent in {time.monotonic() - t0:.1f} s, checking MD5 ...")

    mcu.send("ota!")
    if not mcu.wait(lambda w: w[:2] == ["ok", "ota!"], 10):
        raise SystemExit("MCU did not confirm the image; old firmware still runs")
    time.sleep(1.5)
    new = hello(mcu, 15)                     # this valid line also marks the new image as good
    if not new:
        raise SystemExit("no hello after the restart: the ESP32 rolls back to the old firmware "
                         "within 60 s; check with `make bench-mcu T=watch`")
    print(f"now running: {new}")


if __name__ == "__main__":
    main()
