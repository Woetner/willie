"""Pretend to be the ESP32 on a pseudo-terminal (for tests and `make run-local` without hardware).

    python tools/fake_mcu.py [link-path]   -> prints e.g. /dev/pts/5 ; point link.port at it

Speaks the same protocol as firmware/src/main.cpp (WILL-E.md §5.2): ping/hello, drive/pwm/stop
with the 200 ms watchdog and the PWM cap, look with slew, cfg, clear, stat?, and the 50 Hz `st`
stream with simulated encoders. Sensors read as "not connected", except a fake ToF and battery.
"""
import math
import os
import pty
import select
import sys
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from willie.hal import proto  # noqa: E402

PWM_CAP_HARD = 50.0
CFG = {"pwm_cap": 50, "wd_ms": 200, "v_full": 600, "wheel_d": 100, "track": 170, "cpr": 960,
       "pan_min": -90, "pan_max": 90, "tilt_min": -30, "tilt_max": 45, "servo_dps": 180,
       "stream_hz": 50}
LIMITS = {"pwm_cap": (0, PWM_CAP_HARD), "wd_ms": (50, 1000), "stream_hz": (0, 100)}

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
ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
send = lambda *w: os.write(master, proto.encode(*w))  # noqa: E731

pwm = [0.0, 0.0]
ticks = [0.0, 0.0]
last_motion = 0
moving = False
estop = 0
wd_trips = 0
pan = tilt = 0.0
pan_t = tilt_t = 0.0
x = y = th = 0.0


def cap(v):
    c = min(CFG["pwm_cap"], PWM_CAP_HARD)
    return max(-c, min(c, v))


def motion(left, right):
    global last_motion, moving
    if estop:
        send("err", "estop", "%02X" % estop)
        pwm[:] = [0, 0]
        return
    pwm[:] = [cap(left), cap(right)]
    last_motion = ms()
    moving = any(pwm)


def handle(w):
    global pan_t, tilt_t, estop, moving, x, y, th
    cmd, args = w[0], w[1:]
    try:
        if cmd == "ping":
            send("pong", args[0] if args else 0, ms())
        elif cmd == "hello?":
            send("hello", "willie-fw", "0.2.0", "fake")
        elif cmd == "pwm":
            motion(float(args[0]), float(args[1]))
        elif cmd == "drive":
            v, om = float(args[0]), float(args[1])
            half, full = om * CFG["track"] / 2000, CFG["v_full"] / 1000
            motion((v - half) / full * 100, (v + half) / full * 100)
        elif cmd == "stop":
            pwm[:] = [0, 0]
            moving = False
        elif cmd == "look":
            pan_t = max(CFG["pan_min"], min(CFG["pan_max"], float(args[0])))
            tilt_t = max(CFG["tilt_min"], min(CFG["tilt_max"], float(args[1])))
        elif cmd == "cfg":
            lo, hi = LIMITS.get(args[0], (-1e9, 1e9))
            CFG[args[0]] = max(lo, min(hi, float(args[1])))
            send("cfg", args[0], "%g" % CFG[args[0]])
        elif cmd == "cfg?":
            for k, v in CFG.items():
                send("cfg", k, "%g" % v)
        elif cmd == "clear":
            estop = 0
            send("ok", "clear")
        elif cmd == "odo0":
            x = y = th = 0.0
            send("ok", "odo0")
        elif cmd == "stat?":
            send("stat", "pcf", 0, "mpu", 0, "ina", 0, "tof", "001", "enc", 1, "estop", "%02X" % estop,
                 "wd_trips", wd_trips, "bad_lines", 0, "i2c_err", 0)
        elif cmd in ("led", "cal"):
            send("ok", cmd)
        else:
            send("err", "unknown", cmd)
    except (IndexError, ValueError):
        send("err", "args")


def tick(dt):
    """Physics + watchdog + one `st` line."""
    global moving, wd_trips, pan, tilt, x, y, th
    dt = max(dt, 1e-3)
    if moving and ms() - last_motion > CFG["wd_ms"]:
        pwm[:] = [0, 0]
        moving = False
        wd_trips += 1
        send("ev", "wd", ms())
    per_pct = CFG["v_full"] / 100 / (math.pi * CFG["wheel_d"]) * CFG["cpr"]  # ticks/s per %
    d = [p * per_pct * dt for p in pwm]
    ticks[0] += d[0]
    ticks[1] += d[1]
    mm = [t * math.pi * CFG["wheel_d"] / CFG["cpr"] for t in d]
    dist, dth = sum(mm) / 2, (mm[1] - mm[0]) / CFG["track"]
    x += dist * math.cos(th + dth / 2)
    y += dist * math.sin(th + dth / 2)
    th = math.remainder(th + dth, 2 * math.pi)
    step = CFG["servo_dps"] * dt
    pan += max(-step, min(step, pan_t - pan))
    tilt += max(-step, min(step, tilt_t - tilt))
    flags = estop | 1 << 10 | 1 << 11 | int(moving) << 12  # ToF right + encoders "ok"
    send("st", ms(), int(ticks[0]), int(ticks[1]), int(x), int(y), int(th * 1000),
         int(dist / dt), int(dth / dt * 1000), 0, 0, 800, "FF", 12400, 150 + int(abs(sum(pwm)) * 10),
         0, 0, 1000, 0, 0, 0, 0, int(pan * 10), int(tilt * 10), int(pwm[0]), int(pwm[1]),
         "%X" % flags, 0)


send("hello", "willie-fw", "0.2.0", "fake")
buf = b""
next_st = time.monotonic()
last = time.monotonic()
while True:
    period = 1 / CFG["stream_hz"] if CFG["stream_hz"] else 0.02
    r, _, _ = select.select([master], [], [], max(0.0, next_st - time.monotonic()))
    if r:
        buf += os.read(master, 1024)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                handle(proto.decode(line))
            except proto.BadLine:
                send("err", "crc")
    now = time.monotonic()
    if now >= next_st:
        next_st = now + period
        if CFG["stream_hz"]:
            tick(now - last)
            last = now
