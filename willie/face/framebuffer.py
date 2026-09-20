"""Tiny direct Linux framebuffer renderer for WILL-E's SPI face.

This deliberately uses only the standard library. It is intended for the 480x320
ILI9486 but reads the framebuffer geometry and colour layout at runtime.
"""

from __future__ import annotations

import fcntl
import glob
import mmap
import os
import struct
from dataclasses import dataclass


FBIOGET_VSCREENINFO = 0x4600


@dataclass
class Framebuffer:
    path: str
    fd: int
    memory: mmap.mmap
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

    def close(self) -> None:
        self.memory.close()
        os.close(self.fd)

    def _pixel(self, colour: tuple[int, int, int]) -> bytes:
        value = 0
        for component, (offset, length) in zip(colour, (self.red, self.green, self.blue)):
            if length:
                value |= (component * ((1 << length) - 1) // 255) << offset
        return value.to_bytes(self.bytes_per_pixel, "little")

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
