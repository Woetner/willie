#!/usr/bin/env python3
"""B5 bench test: ILI9486 face screen + XPT2046 touch.

Done when: a test face (two eyes) blinks at >= 20 fps, and a touch prints its x,y.
Run on the Pi:  sudo .venv/bin/python tools/bench/screen.py   (or `make bench-screen`)

Two numbers matter for the fps:
  render fps  - how fast Python can draw into /dev/fb1 (the CPU side)
  panel fps   - how fast SPI can push the changed rows to the glass. fbtft only
                sends the rows that changed, so we only redraw the eye band.
The panel shows the lower of the two.
"""

from __future__ import annotations

import glob
import os
import select
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from willie.face.framebuffer import Framebuffer  # noqa: E402

BLACK, CYAN = (0, 0, 0), (57, 208, 255)
BLINK_FRAMES = (1.0, 0.6, 0.25, 0.05, 0.25, 0.6, 1.0)  # eye openness per frame


def find_panel() -> str:
    for name_file in sorted(glob.glob("/sys/class/graphics/fb*/name")):
        if "ili9486" in Path(name_file).read_text().lower():
            return "/dev/" + Path(name_file).parent.name
    sys.exit("FAIL no ILI9486 framebuffer - is dtoverlay=piscreen enabled in /boot/firmware/config.txt?")


def spi_hz() -> int | None:
    for f in glob.glob("/proc/device-tree/soc/spi@*/*/spi-max-frequency"):
        compat = Path(f).with_name("compatible")
        if compat.exists() and b"9486" in compat.read_bytes():
            return struct.unpack(">I", Path(f).read_bytes()[:4])[0]
    return None


def blink_test(fb: Framebuffer, seconds: float = 10.0) -> None:
    w, h = fb.width, fb.height
    rx, ry = w // 10, h // 5
    eyes = ((w // 3, h // 2), (2 * w // 3, h // 2))
    band_y, band_h = h // 2 - ry - 2, 2 * ry + 5  # only these rows change

    fb.fill(BLACK)
    frames, start, i = 0, time.monotonic(), 0
    while time.monotonic() - start < seconds:
        openness = BLINK_FRAMES[i % len(BLINK_FRAMES)]
        fb.rect(0, band_y, w, band_h, BLACK)
        for cx, cy in eyes:
            fb.ellipse(cx, cy, rx, max(1, int(ry * openness)), CYAN)
        frames += 1
        i += 1
    elapsed = time.monotonic() - start
    for cx, cy in eyes:  # leave the face open-eyed
        fb.ellipse(cx, cy, rx, ry, CYAN)

    render_fps = frames / elapsed
    band_bytes = band_h * fb.stride
    print(f"  render fps : {render_fps:.1f}  ({frames} frames in {elapsed:.1f} s)")
    hz = spi_hz()
    if hz:
        # ~15 % protocol overhead for fbtft (commands, DC toggles, gaps)
        panel_fps = hz / (band_bytes * 8 * 1.15)
        print(f"  SPI clock  : {hz / 1e6:.0f} MHz -> panel fps for the eye band ({band_h} rows) ~ {panel_fps:.1f}")
        best = min(render_fps, panel_fps)
    else:
        print("  SPI clock  : unknown (could not read the device tree) - judge by eye")
        best = render_fps
    verdict = "PASS" if best >= 20 else "FAIL"
    print(f"  {verdict}  blink ~{best:.1f} fps (target >= 20). Also check by eye: smooth, no tearing.")


def find_touch() -> str | None:
    block = ""
    for line in Path("/proc/bus/input/devices").read_text().splitlines() + [""]:
        if line:
            block += line + "\n"
            continue
        if "ADS7846" in block:
            for word in block.split():
                if word.startswith("event"):
                    return "/dev/input/" + word
        block = ""
    return None


def touch_test(seconds: float = 20.0) -> None:
    dev = find_touch()
    if not dev:
        print("  FAIL no ADS7846/XPT2046 input device (touch part of the piscreen overlay missing?)")
        return
    print(f"  touch device {dev}: tap the screen a few times ({seconds:.0f} s)...")
    fmt = "llHHi"  # struct input_event on 64-bit
    size = struct.calcsize(fmt)
    x = y = None
    touching, taps = False, 0
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            data = os.read(fd, size * 64)
            for off in range(0, len(data) - size + 1, size):
                _, _, etype, code, value = struct.unpack_from(fmt, data, off)
                if etype == 3 and code == 0:
                    x = value
                elif etype == 3 and code == 1:
                    y = value
                elif etype == 1 and code == 330:  # BTN_TOUCH
                    touching = bool(value)
                    if not touching and x is not None:
                        taps += 1
                        print(f"    tap {taps}: x={x} y={y}  (raw ADC 0..4095)")
    finally:
        os.close(fd)
    print(f"  {'PASS' if taps else 'FAIL'}  {taps} taps read")


def main() -> int:
    path = find_panel()
    fb = Framebuffer.open(path)
    print(f"== B5 screen test: {path} {fb.width}x{fb.height} {fb.bytes_per_pixel * 8} bpp ==")
    try:
        print("-- 1. blink (10 s)")
        blink_test(fb)
        print("-- 2. touch")
        touch_test()
    finally:
        fb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
