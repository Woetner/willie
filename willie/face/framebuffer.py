"""Tiny direct Linux framebuffer renderer for WILL-E's SPI face.

This deliberately uses only the standard library. It is intended for the 480x320
ILI9486 but reads the framebuffer geometry and colour layout at runtime.
"""

from __future__ import annotations

import array
import fcntl
import glob
import mmap
import os
import struct
import sys
from dataclasses import dataclass


FBIOGET_VSCREENINFO = 0x4600


@dataclass
class Framebuffer:
    path: str
    fd: int
    memory: mmap.mmap | bytearray
    width: int
    height: int
    stride: int
    bytes_per_pixel: int
    red: tuple[int, int]
    green: tuple[int, int]
    blue: tuple[int, int]

    @classmethod
    def open(cls, path: str) -> "Framebuffer":
        fd = os.open(path, os.O_RDWR)
        try:
            info = bytearray(160)
            fcntl.ioctl(fd, FBIOGET_VSCREENINFO, info, True)
            width, height, virtual_width, virtual_height, _, _, bits = struct.unpack_from("7I", info)
            if not width or not height or bits not in (16, 24, 32):
                raise RuntimeError(f"unsupported framebuffer: {width}x{height}, {bits} bpp")
            # fb_var_screeninfo bitfields begin at byte 32: offset, length, msb_right.
            def field(offset: int) -> tuple[int, int]:
                bit_offset, length, _ = struct.unpack_from("3I", info, offset)
                return bit_offset, length

            bpp = bits // 8
            stride = virtual_width * bpp
            memory = mmap.mmap(fd, stride * virtual_height, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            return cls(path, fd, memory, width, height, stride, bpp, field(32), field(44), field(56))
        except Exception:
            os.close(fd)
            raise

    @classmethod
    def canvas(cls, width: int = 480, height: int = 320) -> "Framebuffer":
        """RGB565 software surface; no device, display server or Pi dependency."""
        return cls("memory", -1, bytearray(width * height * 2), width, height,
                   width * 2, 2, (11, 5), (5, 6), (0, 5))

    def back_buffer(self) -> "Framebuffer":
        return Framebuffer("memory", -1, bytearray(self.stride * self.height),
                           self.width, self.height, self.stride, self.bytes_per_pixel,
                           self.red, self.green, self.blue)

    def present(self, frame: "Framebuffer", bands=None) -> int:
        """Only touch changed rows: fbtft transfers the dirty band over SPI.

        Drawing finishes in RAM first so the panel cannot see an erased half-face.
        `bands` = [(first row, end row), ...] limits the compare to rows the renderer
        repainted (Renderer.bands); None compares every row.
        Returns the number of rows written (useful for the offline performance test).
        """
        if (self.width, self.height, self.stride, self.bytes_per_pixel,
            self.red, self.green, self.blue) != (
                frame.width, frame.height, frame.stride, frame.bytes_per_pixel,
                frame.red, frame.green, frame.blue):
            raise ValueError("framebuffer layout mismatch")
        changed = 0
        row_bytes = self.width * self.bytes_per_pixel
        for lo, hi in ((0, self.height),) if bands is None else bands:
            for y in range(max(0, lo), min(self.height, hi)):
                start = y * self.stride
                row = frame.memory[start:start + row_bytes]
                if self.memory[start:start + row_bytes] != row:
                    self.memory[start:start + row_bytes] = row
                    changed += 1
        return changed

    def close(self) -> None:
        if self.fd >= 0:
            self.memory.close()
            os.close(self.fd)
            self.fd = -1

    def _pixel(self, colour: tuple[int, int, int]) -> bytes:
        cache = self.__dict__.setdefault("_pixels", {})
        pixel = cache.get(colour)
        if pixel is None:
            if len(cache) > 256:
                cache.clear()
            pixel = cache[colour] = self._encode(colour)
        return pixel

    def _encode(self, colour: tuple[int, int, int]) -> bytes:
        value = 0
        for component, (offset, length) in zip(colour, (self.red, self.green, self.blue)):
            if length:
                value |= (component * ((1 << length) - 1) // 255) << offset
        return value.to_bytes(self.bytes_per_pixel, "little")

    def layout(self) -> tuple:
        """Pixel format key: pictures are converted once per format, not per frame."""
        return (self.bytes_per_pixel, self.red, self.green, self.blue)

    def convert(self, rgb: bytes) -> bytes:
        """Packed 8-bit RGB -> this framebuffer's pixel format (one-off, for pictures)."""
        (ro, rl), (go, gl), (bo, bl) = self.red, self.green, self.blue
        rs, gs, bs = 8 - rl, 8 - gl, 8 - bl
        try:
            import numpy as np
            px = np.frombuffer(rgb[: len(rgb) // 3 * 3], np.uint8).reshape(-1, 3).astype(np.uint32)
            value = ((px[:, 0] >> rs) << ro) | ((px[:, 1] >> gs) << go) | ((px[:, 2] >> bs) << bo)
            if self.bytes_per_pixel in (2, 4):
                return value.astype("<u2" if self.bytes_per_pixel == 2 else "<u4").tobytes()
        except ImportError:
            pass
        values = [((rgb[i] >> rs) << ro) | ((rgb[i + 1] >> gs) << go) | ((rgb[i + 2] >> bs) << bo)
                  for i in range(0, len(rgb) - 2, 3)]
        size = self.bytes_per_pixel
        if size == 2:
            return array.array("H", values).tobytes() if sys.byteorder == "little" else \
                b"".join(v.to_bytes(2, "little") for v in values)
        return b"".join(v.to_bytes(size, "little") for v in values)

    def blit(self, x: int, y: int, width: int, height: int, pixels: bytes) -> None:
        """Copy converted pixels (see convert) with the top-left corner at x, y."""
        row_bytes = width * self.bytes_per_pixel
        for row in range(height):
            py = y + row
            if not 0 <= py < self.height:
                continue
            x0, x1 = max(0, x), min(self.width, x + width)
            if x0 >= x1:
                return
            src = row * row_bytes + (x0 - x) * self.bytes_per_pixel
            dst = py * self.stride + x0 * self.bytes_per_pixel
            n = (x1 - x0) * self.bytes_per_pixel
            self.memory[dst:dst + n] = pixels[src:src + n]

    def fill(self, colour: tuple[int, int, int]) -> None:
        row = self._pixel(colour) * self.width
        padding = self.stride - len(row)
        for y in range(self.height):
            start = y * self.stride
            self.memory[start : start + len(row)] = row
            if padding:
                self.memory[start + len(row) : start + self.stride] = b"\0" * padding

    def rect(self, x: int, y: int, width: int, height: int, colour: tuple[int, int, int]) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + width), min(self.height, y + height)
        if x0 >= x1 or y0 >= y1:
            return
        pixel = self._pixel(colour)
        row = pixel * (x1 - x0)
        for py in range(y0, y1):
            start = py * self.stride + x0 * self.bytes_per_pixel
            self.memory[start : start + len(row)] = row

    def ellipse(self, cx: int, cy: int, rx: int, ry: int, colour: tuple[int, int, int]) -> None:
        if rx <= 0 or ry <= 0:
            return
        pixel = self._pixel(colour)
        for dy in range(-ry, ry + 1):
            span = int(rx * (max(0, 1 - (dy * dy) / (ry * ry)) ** 0.5))
            x0, x1 = max(0, cx - span), min(self.width, cx + span + 1)
            if 0 <= cy + dy < self.height and x0 < x1:
                start = (cy + dy) * self.stride + x0 * self.bytes_per_pixel
                self.memory[start : start + (x1 - x0) * self.bytes_per_pixel] = pixel * (x1 - x0)

    def round_rect(self, x0: int, y0: int, x1: int, y1: int, r: int, colour: tuple[int, int, int]) -> None:
        """Filled rectangle [x0, x1) x [y0, y1) with corner radius r, one span per row."""
        self._round_rect(x0, y0, x1, y1, r, colour, 0, self.height)

    def _round_rect(self, x0, y0, x1, y1, r, colour, lo, hi):
        pixel, bpp = self._pixel(colour), self.bytes_per_pixel
        r = max(0, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
        for py in range(max(y0, lo, 0), min(y1, hi, self.height)):
            d = max(y0 + r - py, py - (y1 - 1 - r), 0)
            inset = r - int(r * max(0, 1 - (d * d) / (r * r)) ** 0.5) if d else 0
            a, b = max(0, x0 + inset), min(self.width, x1 - inset)
            if a < b:
                start = py * self.stride + a * bpp
                self.memory[start:start + (b - a) * bpp] = pixel * (b - a)

    def paint(self, ops, bands=None) -> None:
        """Replay recorded draw calls (see Recorder), clipped to row bands.

        The final colour of a row depends only on the ops that touch it, in order, so
        replaying every op clipped to [lo, hi) gives the same rows as a full repaint.
        """
        for lo, hi in ((0, self.height),) if bands is None else bands:
            for op in ops:
                if op[2] > lo and op[1] < hi:
                    self._op(op, max(lo, op[1]), min(hi, op[2]))

    def _op(self, op, lo, hi):
        kind, bpp, stride = op[0], self.bytes_per_pixel, self.stride
        if kind == "g":                                   # group (a cached text string)
            for sub in op[3]:
                if sub[2] > lo and sub[1] < hi:
                    self._op(sub, max(lo, sub[1]), min(hi, sub[2]))
        elif kind == "r":
            _, _, _, x0, x1, colour = op
            row = self._pixel(colour) * (x1 - x0)
            for py in range(lo, hi):
                start = py * stride + x0 * bpp
                self.memory[start:start + len(row)] = row
        elif kind == "e":
            _, _, _, cx, cy, rx, ry, colour = op
            pixel = self._pixel(colour)
            for py in range(lo, hi):
                dy = py - cy
                span = int(rx * (max(0, 1 - (dy * dy) / (ry * ry)) ** 0.5))
                x0, x1 = max(0, cx - span), min(self.width, cx + span + 1)
                if x0 < x1:
                    start = py * stride + x0 * bpp
                    self.memory[start:start + (x1 - x0) * bpp] = pixel * (x1 - x0)
        elif kind == "q":
            _, _, _, x0, x1, r, colour, y0, y1 = op
            self._round_rect(x0, y0, x1, y1, r, colour, lo, hi)
        elif kind == "f":
            row = self._pixel(op[3]) * self.width
            padding = b"\0" * (stride - len(row))
            for py in range(lo, hi):
                start = py * stride
                self.memory[start:start + len(row)] = row
                if padding:
                    self.memory[start + len(row):start + stride] = padding
        elif kind == "b":
            _, _, _, x, y, width, height, pixels = op
            x0, x1 = max(0, x), min(self.width, x + width)
            n, row_bytes = (x1 - x0) * bpp, width * bpp
            for py in range(lo, hi):
                src = (py - y) * row_bytes + (x0 - x) * bpp
                dst = py * stride + x0 * bpp
                self.memory[dst:dst + n] = pixels[src:src + n]


class Recorder:
    """Stands in for a Framebuffer while the renderer composes a frame: it records
    clipped draw calls instead of touching pixels. Each op is (kind, first row, end
    row, ...), so the renderer can compare two frames and repaint only the rows whose
    ops changed (D6: the face drew every pixel of every frame and cost ~70 % CPU)."""

    def __init__(self, target):
        self.width, self.height = target.width, target.height
        self.bytes_per_pixel = target.bytes_per_pixel
        self._layout = target.layout() if hasattr(target, "layout") else None
        self.ops: list[tuple] = []

    def layout(self):
        return self._layout

    def fill(self, colour):
        self.ops.append(("f", 0, self.height, colour))

    def rect(self, x, y, width, height, colour):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + width), min(self.height, y + height)
        if x0 < x1 and y0 < y1:
            self.ops.append(("r", y0, y1, x0, x1, colour))

    def ellipse(self, cx, cy, rx, ry, colour):
        y0, y1 = max(0, cy - ry), min(self.height, cy + ry + 1)
        if rx > 0 and ry > 0 and y0 < y1 and cx + rx >= 0 and cx - rx < self.width:
            self.ops.append(("e", y0, y1, cx, cy, rx, ry, colour))

    def round_rect(self, x0, y0, x1, y1, r, colour):
        a, b = max(0, y0), min(self.height, y1)
        if x0 < x1 and a < b and x1 > 0 and x0 < self.width:
            # The unclipped y0/y1 ride along at the end: corners depend on them.
            self.ops.append(("q", a, b, x0, x1, r, colour, y0, y1))

    def blit(self, x, y, width, height, pixels):
        y0, y1 = max(0, y), min(self.height, y + height)
        if y0 < y1 and max(0, x) < min(self.width, x + width):
            self.ops.append(("b", y0, y1, x, y, width, height, pixels))


def changed_bands(old, new, height, gap=8):
    """Rows that differ between two op lists: everything touched by the ops between
    their common prefix and common suffix. Ops outside that middle part are equal and
    in the same order on both sides, so rows only they touch look the same."""
    if old is None:
        return [(0, height)]
    n = min(len(old), len(new))
    p = 0
    while p < n and old[p] == new[p]:
        p += 1
    if p == len(old) == len(new):
        return []
    s = 0
    while s < n - p and old[-1 - s] == new[-1 - s]:
        s += 1
    spans = sorted((op[1], op[2]) for op in old[p:len(old) - s] + new[p:len(new) - s])
    bands: list[list[int]] = []
    for lo, hi in spans:
        if bands and lo <= bands[-1][1] + gap:
            bands[-1][1] = max(bands[-1][1], hi)
        else:
            bands.append([lo, hi])
    return [tuple(b) for b in bands]


def open_all() -> list[Framebuffer]:
    """Open every usable framebuffer so the face reaches SPI and/or HDMI."""
    displays = []
    for path in glob.glob("/dev/fb*"):
        try:
            displays.append(Framebuffer.open(path))
        except (OSError, RuntimeError):
            continue
    if not displays:
        raise RuntimeError("No usable /dev/fb* device. Enable the ILI9486 framebuffer overlay first.")
    return displays
