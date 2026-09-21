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

    def present(self, frame: "Framebuffer") -> int:
        """Only touch changed rows: fbtft transfers the dirty band over SPI.

        Drawing finishes in RAM first so the panel cannot see an erased half-face.
        Returns the number of rows written (useful for the offline performance test).
        """
        if (self.width, self.height, self.stride, self.bytes_per_pixel,
            self.red, self.green, self.blue) != (
                frame.width, frame.height, frame.stride, frame.bytes_per_pixel,
                frame.red, frame.green, frame.blue):
            raise ValueError("framebuffer layout mismatch")
        changed = 0
        for y in range(self.height):
            start, end = y * self.stride, y * self.stride + self.width * self.bytes_per_pixel
            row = frame.memory[start:end]
            if self.memory[start:end] != row:
                self.memory[start:end] = row
                changed += 1
        return changed

    def close(self) -> None:
        if self.fd >= 0:
            self.memory.close()
            os.close(self.fd)
            self.fd = -1

    def _pixel(self, colour: tuple[int, int, int]) -> bytes:
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
