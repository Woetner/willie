"""Pretend to be the ESP32 on a pseudo-terminal (for tests and `make run-local` without hardware).

    python tools/fake_mcu.py          -> prints e.g. /dev/pts/5 ; point link.port at it
Answers `ping N` with `pong N <ms>` and `hello?` with a hello line, like firmware/src/main.cpp.
"""
import os
import pty
import sys
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from willie.hal import proto  # noqa: E402

master, slave = pty.openpty()
tty.setraw(slave)  # no echo / no line editing, like a real UART
path = os.ttyname(slave)
link = sys.argv[1] if len(sys.argv) > 1 else None
if link:  # stable path for scripts
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(path, link)
print(link or path, flush=True)
t0 = time.monotonic()
buf = b""
os.write(master, proto.encode("hello", "willie-fw", "0.1.0", "fake"))
while True:
    buf += os.read(master, 1024)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        try:
            w = proto.decode(line)
        except proto.BadLine:
            os.write(master, proto.encode("err", "crc"))
            continue
        if w[0] == "ping":
            os.write(master, proto.encode("pong", w[1], int((time.monotonic() - t0) * 1000)))
        elif w[0] == "hello?":
            os.write(master, proto.encode("hello", "willie-fw", "0.1.0", "fake"))
        else:
            os.write(master, proto.encode("err", "unknown", w[0]))
