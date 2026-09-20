"""Pi <-> MCU line protocol (D18, WILL-E.md §5.2). Same rules as firmware/src/proto.h.

One message per line, human-readable, CRC per line:

    <payload>*<CRC8 as 2 uppercase hex digits>\\n        e.g.  ping 17*3C

CRC-8, polynomial 0x07, init 0x00, no reflection, over the payload bytes only.
Payload = words separated by single spaces; the first word is the message type.
You can type messages by hand in a serial monitor with `crc` from this module:

    python -m willie.hal.proto "ping 1"      ->  ping 1*..
"""
from __future__ import annotations

import sys


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode(*words) -> bytes:
    payload = " ".join(str(w) for w in words).encode("ascii")
    return payload + b"*%02X\n" % crc8(payload)


class BadLine(ValueError):
    pass


def decode(line: bytes) -> list[str]:
    """Return the words of a valid line, or raise BadLine."""
    line = line.strip()
    star = line.rfind(b"*")
    if star < 1 or len(line) - star != 3:
        raise BadLine(f"no CRC: {line[:40]!r}")
    payload, crc_hex = line[:star], line[star + 1:]
    try:
        crc = int(crc_hex, 16)
    except ValueError:
        raise BadLine(f"bad CRC field: {line[:40]!r}") from None
    if crc != crc8(payload):
        raise BadLine(f"CRC mismatch: {line[:40]!r}")
    return payload.decode("ascii", "replace").split()


if __name__ == "__main__":
    print(encode(*" ".join(sys.argv[1:]).split()).decode().strip())
