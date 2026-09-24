"""MCU serial link (D18): connect, ping/pong round-trip, the 50 Hz `st` state stream, commands.

Messages are listed in WILL-E.md §5.2; the `st` field order is ST_FIELDS (= firmware sendState()).

CLI test (A9 "done when"), on the Pi with the core stopped:
    .venv/bin/python -m willie.hal.link --port /dev/serial0 -n 200
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

import serial  # pyserial

from willie.hal import proto

log = logging.getLogger("willie.link")


# `st` line from the firmware, in order (firmware/src/main.cpp sendState()).
ST_FIELDS = ("ms", "ticks_l", "ticks_r", "x_mm", "y_mm", "th_mrad", "v_mms", "w_mrads",
             "tof_l", "tof_c", "tof_r", "io", "mv", "ma", "ax", "ay", "az", "gx", "gy", "gz",
             "tilt_d10", "pan_d10", "tilt_servo_d10", "pwm_l", "pwm_r", "flags", "i2c_err")
_HEX = {"io", "flags"}
ESTOP_BITS = ("bump_l", "bump_r", "cliff_l", "cliff_r", "tilt")
OK_BITS = {"pcf": 5, "mpu": 6, "ina": 7, "tof_l": 8, "tof_c": 9, "tof_r": 10, "enc": 11}


def parse_state(words: list[str]) -> dict:
    """`st ...` words -> dict of ints, plus decoded `estop` list, `ok` dict and `moving`."""
    if len(words) != len(ST_FIELDS) + 1:
        raise proto.BadLine(f"st has {len(words) - 1} fields, want {len(ST_FIELDS)}")
    st = {k: int(v, 16 if k in _HEX else 10) for k, v in zip(ST_FIELDS, words[1:])}
    f = st["flags"]
    st["estop"] = [n for i, n in enumerate(ESTOP_BITS) if f >> i & 1]
    st["ok"] = {n: bool(f >> b & 1) for n, b in OK_BITS.items()}
    st["moving"] = bool(f >> 12 & 1)
    return st


# willie.yaml -> firmware `cfg` keys (firmware/src/config.h). The firmware clamps every value,
# so nothing here can lift the 50 % PWM cap or the watchdog limits (§10.2).
MCU_SETTINGS = {
    "pwm_cap": "drive.pwm_cap", "wheel_d": "drive.wheel_diameter", "track": "drive.track_width",
    "cpr": "drive.encoder_cpr", "wd_ms": "safety.watchdog_ms", "tilt_stop": "safety.tilt_stop_deg",
    "pid_on": "drive.pid_on", "kp": "drive.pid_kp", "ki": "drive.pid_ki", "kd": "drive.pid_kd",
    "pan_min": "head.pan_min_deg", "pan_max": "head.pan_max_deg", "tilt_min": "head.tilt_min_deg",
    "tilt_max": "head.tilt_max_deg", "servo_dps": "head.speed_dps",
    "pan_c": ("head.pan_trim_us", 1500), "tilt_c": ("head.tilt_trim_us", 1500),
}


def mcu_settings(get) -> dict:
    """`get(dotted)` from willie.config.Config -> {firmware key: value}."""
    out = {}
    for key, src in MCU_SETTINGS.items():
        out[key] = get(src[0]) + src[1] if isinstance(src, tuple) else get(src)
    return out


@dataclass
class LinkStats:
    port: str = ""
    connected: bool = False
    firmware: str | None = None      # from the MCU's "hello" line
    pings: int = 0
    pongs: int = 0
    bad_lines: int = 0
    rtt_last_ms: float | None = None
    rtt_ms: deque = field(default_factory=lambda: deque(maxlen=200))

    def summary(self) -> dict:
        r = list(self.rtt_ms)
        return {
            "port": self.port,
            "connected": self.connected,
            "firmware": self.firmware,
            "pings": self.pings,
            "pongs": self.pongs,
            "lost": self.pings - self.pongs,
            "bad_lines": self.bad_lines,
            "rtt_last_ms": self.rtt_last_ms,
            "rtt_avg_ms": round(statistics.fmean(r), 3) if r else None,
            "rtt_p95_ms": round(sorted(r)[int(len(r) * 0.95) - 1], 3) if len(r) >= 20 else None,
            "rtt_max_ms": round(max(r), 3) if r else None,
        }


class Link:
    def __init__(self, port: str, baud: int = 921600, ping_hz: float = 2.0, on_message=None):
        self.port, self.baud, self.ping_hz = port, baud, ping_hz
        self.on_message = on_message          # fn(words) for everything that isn't pong/hello/st
        self.on_hello = None                  # fn() after the MCU (re)starts, e.g. to push settings
        self.state: dict | None = None        # last `st` line, parsed
        self.state_t = 0.0                    # perf_counter() when it arrived
        self.states = 0                       # `st` lines received
        self.clock_offset_ms: float | None = None   # Pi perf_counter ms - MCU millis(), from pings
        self.stats = LinkStats(port=port)
        self._ser: serial.Serial | None = None
        self._buf = bytearray()
        self._seq = 0
        self._sent: dict[int, float] = {}
        self._pong_waiters: dict[int, asyncio.Future] = {}

    # ---- transport -------------------------------------------------------
    def _open(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=0, write_timeout=0.05)
        self._ser.reset_input_buffer()
        asyncio.get_running_loop().add_reader(self._ser.fileno(), self._on_readable)
        self.stats.connected = True
        log.info("link open %s @ %d", self.port, self.baud)

    def _close(self):
        if self._ser:
            try:
                asyncio.get_running_loop().remove_reader(self._ser.fileno())
            except Exception:
                pass
            self._ser.close()
        self._ser = None
        self.stats.connected = False

    def send(self, *words):
        if not self._ser:
            raise ConnectionError("link not open")
        self._ser.write(proto.encode(*words))

    def _on_readable(self):
        try:
            data = self._ser.read(4096)
        except (serial.SerialException, OSError) as e:
            log.warning("link read error: %s", e)
            self._close()
            return
        self._buf += data
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                if len(self._buf) > 512:        # garbage without newlines
                    self._buf.clear()
                    self.stats.bad_lines += 1
                return
            line, self._buf = bytes(self._buf[:nl]), self._buf[nl + 1:]
            if line.strip():
                self._handle(line)

    def _handle(self, line: bytes):
        now = time.perf_counter()
        try:
            words = proto.decode(line)
        except proto.BadLine as e:
            self.stats.bad_lines += 1
            log.debug("%s", e)
            return
        kind = words[0]
        if kind == "pong" and len(words) >= 2 and words[1].isdigit():
            seq = int(words[1])
            t0 = self._sent.pop(seq, None)
            if t0 is not None:
                rtt = (now - t0) * 1000
                self.stats.pongs += 1
                self.stats.rtt_last_ms = round(rtt, 3)
                self.stats.rtt_ms.append(rtt)
                if self.stats.pongs == 1:
                    log.info("first pong from MCU: %.2f ms", rtt)
                if len(words) >= 3 and words[2].isdigit():   # MCU clock at the midpoint
                    self.clock_offset_ms = (t0 * 1000 + rtt / 2) - int(words[2])
            fut = self._pong_waiters.pop(seq, None)
            if fut and not fut.done():
                fut.set_result(now)
        elif kind == "st":
            try:
                self.state = parse_state(words)
            except (proto.BadLine, ValueError) as e:
                self.stats.bad_lines += 1
                log.debug("%s", e)
                return
            self.state_t = now
            self.states += 1
        elif kind == "hello":
            self.stats.firmware = " ".join(words[1:])
            log.info("MCU says hello: %s", self.stats.firmware)
            if self.on_hello:
                self.on_hello()
        elif self.on_message:
            self.on_message(words)
        elif kind in ("cfg", "ok"):            # echoes of our own commands
            log.debug("MCU: %s", " ".join(words))
        else:
            log.info("MCU: %s", " ".join(words))

    def mcu_to_local_ms(self, mcu_ms: int) -> float | None:
        """MCU millis() -> this process's perf_counter() in ms (needs one pong first)."""
        return None if self.clock_offset_ms is None else mcu_ms + self.clock_offset_ms

    # ---- commands (WILL-E.md §5.2); motion must be repeated within wd_ms -----
    def drive(self, v: float, w: float):
        self.send("drive", f"{v:.3f}", f"{w:.3f}")

    def pwm(self, left: float, right: float):
        self.send("pwm", f"{left:.1f}", f"{right:.1f}")

    def stop(self):
        self.send("stop")

    def look(self, pan: float, tilt: float):
        self.send("look", f"{pan:.1f}", f"{tilt:.1f}")

    def cfg(self, key: str, value: float):
        self.send("cfg", key, f"{value:g}")

    # ---- ping --------------------------------------------------------------
    async def ping(self, timeout: float = 0.1) -> float | None:
        """Send one ping, return the round-trip in ms (None on timeout)."""
        self._seq = (self._seq + 1) % 1_000_000
        seq = self._seq
        fut = asyncio.get_running_loop().create_future()
        self._pong_waiters[seq] = fut
        t0 = time.perf_counter()
        self._sent[seq] = t0
        self.stats.pings += 1
        self.send("ping", seq)
        try:
            t1 = await asyncio.wait_for(fut, timeout)
            return (t1 - t0) * 1000
        except asyncio.TimeoutError:
            self._sent.pop(seq, None)
            self._pong_waiters.pop(seq, None)
            return None

    # ---- long-running task for the core ------------------------------------
    async def run(self, report_every_s: float = 60.0):
        warned = False
        last_report = time.monotonic()
        while True:
            if not self._ser:
                try:
                    self._open()
                    warned = False
                    self.send("hello?")
                except (serial.SerialException, OSError) as e:
                    if not warned:
                        log.warning("MCU link not available (%s) — retrying every 5 s", e)
                        warned = True
                    await asyncio.sleep(5)
                    continue
            await self.ping()
            if time.monotonic() - last_report >= report_every_s and self.stats.pongs:
                s = self.stats.summary()
                log.info("link rtt avg %.2f / p95 %s / max %.2f ms, lost %d, bad %d",
                         s["rtt_avg_ms"], s["rtt_p95_ms"], s["rtt_max_ms"], s["lost"], s["bad_lines"])
                last_report = time.monotonic()
            await asyncio.sleep(1 / self.ping_hz)

    def close(self):
        self._close()


async def _cli(port: str, baud: int, n: int):
    link = Link(port, baud)
    link._open()
    link.send("hello?")
    await asyncio.sleep(0.3)     # let a "hello" from a freshly reset board arrive
    rtts = []
    for _ in range(n):
        r = await link.ping(timeout=0.2)
        if r is not None:
            rtts.append(r)
        await asyncio.sleep(0.01)
    link.close()
    if not rtts:
        print(f"no pong in {n} pings — check wiring (Pi TX->MCU RX, Pi RX<-MCU TX, GND) and baud")
        return 1
    rtts.sort()
    print(f"firmware: {link.stats.firmware}")
    print(f"pongs {len(rtts)}/{n}   min {rtts[0]:.2f}   avg {statistics.fmean(rtts):.2f}   "
          f"p95 {rtts[int(len(rtts) * 0.95) - 1]:.2f}   max {rtts[-1]:.2f} ms   (A9 target: < 5 ms)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="WILL-E MCU link ping test")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("-n", type=int, default=100)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(_cli(a.port, a.baud, a.n)))


if __name__ == "__main__":
    main()
