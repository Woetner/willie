"""B9–B12 bench tests for the ESP32 real-time layer, run on the Pi over the link (D18).

    make bench-mcu T=servo      B9  pan/tilt sweep, then type "pan tilt" to move, "cfg key value" to tune
    make bench-mcu T=sensors    B10 50 Hz stream for 10 min: rate, I2C errors, live values
    make bench-mcu T=io         B11 cliff/bumper events with Pi-side latency
    make bench-mcu T=motors     B12 both wheels both ways, counts per revolution, watchdog stop time
    make bench-mcu T=watch      print every line from the MCU

Directly on the Pi (core stopped):  .venv/bin/python tools/bench/mcu.py servo [--port /dev/serial0]
Against the fake MCU on the Mac:     python tools/fake_mcu.py /tmp/mcu & python tools/bench/mcu.py motors --port /tmp/mcu
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from willie.hal.link import Link  # noqa: E402


async def ainput(prompt: str = "") -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, prompt)


async def connect(port: str, baud: int, on_message=None) -> Link:
    link = Link(port, baud, on_message=on_message)
    link._open()
    link.send("hello?")
    for _ in range(20):
        await link.ping(timeout=0.2)          # also sets the clock offset for latency figures
    if not link.stats.pongs:
        raise SystemExit("no answer from the MCU: check wiring (Pi TX->ESP RX16, Pi RX<-ESP TX17, GND), "
                         "baud, and that the firmware is flashed (make flash)")
    await asyncio.sleep(0.2)
    print(f"firmware: {link.stats.firmware}   rtt {link.stats.rtt_last_ms} ms")
    return link


async def repeat(link: Link, fn, seconds: float, hz: float = 20) -> float:
    """Send a motion command at `hz` for `seconds` (the watchdog needs it every < wd_ms).
    Returns perf_counter() of the last send."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        fn()
        t_sent = time.perf_counter()
        await asyncio.sleep(1 / hz)
    return t_sent


# ---------------------------------------------------------------- B9 servos
async def t_servo(link: Link):
    print("B9: sweeping pan -90..90 and tilt -30..45. Watch for smooth motion and end-stop buzz.")
    for pan, tilt in [(0, 0), (-90, 0), (90, 0), (0, 0), (0, -30), (0, 45), (0, 0)]:
        link.look(pan, tilt)
        print(f"  look {pan} {tilt}")
        await asyncio.sleep(1.5)
    print("After ~1 s at rest the pulses stop (servo_idle): the servos must be silent now.")
    print('Type "pan tilt" to move, "cfg key value" to tune (pan_c, tilt_c, us_deg, pan_min, ...), '
          '"q" to quit.\nIf a servo hums at an end: lower pan_max/tilt_max or move pan_c/tilt_c. '
          "Write the final numbers into WILL-E.md §12.")
    while True:
        line = (await ainput("> ")).strip()
        if line in ("q", "quit", ""):
            break
        w = line.split()
        if w[0] == "cfg" and len(w) == 3:
            link.cfg(w[1], float(w[2]))
        elif len(w) == 2:
            link.look(float(w[0]), float(w[1]))
        else:
            print("?")
        await asyncio.sleep(0.2)


# ---------------------------------------------------------------- B10 sensors
async def t_sensors(link: Link, minutes: float):
    await asyncio.sleep(0.5)
    if not link.state:
        raise SystemExit("no `st` lines: firmware older than 0.2.0?")
    s0, n0, t_start = link.state, link.states, time.monotonic()
    print(f"B10: {minutes:g} min of the 50 Hz stream. Devices: {s0['ok']}")
    missing = [k for k, v in s0["ok"].items() if not v and k != "enc"]
    if missing:
        print(f"  NOT FOUND on I2C: {', '.join(missing)} (B10 needs all of them)")
    n_last, t_last = n0, t_start
    while time.monotonic() - t_start < minutes * 60:
        await asyncio.sleep(1)
        s, now = link.state, time.monotonic()
        rate = (link.states - n_last) / (now - t_last)
        n_last, t_last = link.states, now
        print(f"\r{int(now - t_start):4d}s {rate:5.1f} Hz  i2c_err {s['i2c_err'] - s0['i2c_err']:3d}  "
              f"ToF {s['tof_l']:5d} {s['tof_c']:5d} {s['tof_r']:5d} mm  "
              f"acc {s['ax']:5d} {s['ay']:5d} {s['az']:5d} mg  gz {s['gz'] / 10:6.1f} dps  "
              f"tilt {s['tilt_d10'] / 10:4.1f}  {s['mv'] / 1000:5.2f} V {s['ma']:5d} mA  io {s['io']:02X}",
              end="", flush=True)
    s, dur = link.state, time.monotonic() - t_start
    rate = (link.states - n0) / dur
    errors = s["i2c_err"] - s0["i2c_err"]
    ok = rate >= 49 and errors == 0 and not missing
    print(f"\n{'PASS' if ok else 'FAIL'}: {rate:.1f} Hz average over {dur / 60:.1f} min, "
          f"{errors} I2C errors, missing: {missing or 'none'}, bad lines {link.stats.bad_lines}")


# ---------------------------------------------------------------- B11 cliff + bumper
async def t_io(link: Link, events: list):
    print("B11: press each bumper and lift each cliff sensor off the floor (and back). Ctrl+C to stop.\n"
          "Latency = MCU detection -> Pi arrival (clock synced by pings). Target: estop < 20 ms.")
    worst = 0.0
    try:
        while True:
            await asyncio.sleep(0.05)
            while events:
                t_rx, w = events.pop(0)
                local = link.mcu_to_local_ms(int(w[-1])) if w[-1].isdigit() else None
                lat = t_rx * 1000 - local if local is not None else float("nan")
                if w[0] == "estop":
                    worst = max(worst, lat)
                print(f"  {' '.join(w[:-1]):16s} {lat:6.1f} ms")
                if w[0] == "estop":
                    link.send("clear")        # bench only: re-arm right away
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    print(f"worst estop latency {worst:.1f} ms (+ ≤ 2 ms PCF8574 poll on the MCU)")


# ---------------------------------------------------------------- B12 motors + encoders
async def t_motors(link: Link, pct: float):
    await ainput(f"B12: WHEELS OFF THE GROUND. Motors run at {pct:g} % (capped at 50 % in the firmware). Enter = go ")
    link.send("clear")
    for name, l, r in [("left fwd", pct, 0), ("left back", -pct, 0), ("right fwd", 0, pct), ("right back", 0, -pct)]:
        a = link.state
        await repeat(link, lambda: link.pwm(l, r), 2.0)
        link.stop()
        await asyncio.sleep(0.4)
        b = link.state
        dl, dr = b["ticks_l"] - a["ticks_l"], b["ticks_r"] - a["ticks_r"]
        want = (1 if l > 0 else -1 if l < 0 else 0, 1 if r > 0 else -1 if r < 0 else 0)
        got = tuple((d > 50) - (d < -50) for d in (dl, dr))
        print(f"  {name:10s} ticks L {dl:7d}  R {dr:7d}   {'ok' if got == want else 'CHECK: ' + str(got)}")
    print("A wheel turning the wrong way: `cfg inv_l 1` / `inv_r`. Counts with the wrong sign: `einv_l` / `einv_r`.")

    print("\nCounts per wheel revolution: put a tape mark on the LEFT wheel.")
    await ainput("  Enter to start the wheel slowly; press Enter again after exactly 10 turns ")
    a = link.state["ticks_l"]
    stop = asyncio.Event()
    waiter = asyncio.create_task(ainput())
    waiter.add_done_callback(lambda _: stop.set())
    while not stop.is_set():
        link.pwm(15, 0)
        await asyncio.sleep(0.05)
    link.stop()
    await asyncio.sleep(0.3)
    cpr = abs(link.state["ticks_l"] - a) / 10
    print(f"  ≈ {cpr:.0f} counts per wheel revolution (x4 quadrature). Write it in WILL-E.md §12 and "
          f"config drive.encoder_cpr.")

    print("\nWatchdog: run both wheels, then the Pi goes silent.")
    t_last = await repeat(link, lambda: link.pwm(pct, pct), 1.0)
    while link.state["pwm_l"] or link.state["pwm_r"]:
        await asyncio.sleep(0.002)
        if time.perf_counter() - t_last > 1:
            break
    t_stop = (link.state_t - t_last) * 1000
    ok = t_stop <= 240
    print(f"  {'PASS' if ok else 'FAIL'}: motors braked {t_stop:.0f} ms after the last command "
          f"(wd_ms 200 + ≤ 10 ms timer + ≤ 20 ms until the next `st` frame shows it)")
    print("  Now the real B12 test: run `make bench-mcu T=spin` and pull the Pi's UART plug.")


async def t_spin(link: Link, pct: float):
    await ainput(f"WHEELS OFF THE GROUND. Both wheels at {pct:g} % until Ctrl+C — pull the UART plug to test. Enter = go ")
    link.send("clear")
    try:
        while True:
            link.pwm(pct, pct)
            await asyncio.sleep(0.05)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("test", choices=["servo", "sensors", "io", "motors", "spin", "watch"])
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--pct", type=float, default=30, help="motor duty for B12 (firmware caps at 50)")
    a = ap.parse_args()

    events: list = []

    def on_message(w):
        if a.test == "watch" or w[0] in ("err", "ok", "cfg", "stat"):
            print("  MCU:", " ".join(w))
        if w[0] in ("ev", "estop"):
            events.append((time.perf_counter(), w))

    link = await connect(a.port, a.baud, on_message)
    try:
        if a.test == "servo":
            await t_servo(link)
        elif a.test == "sensors":
            await t_sensors(link, a.minutes)
        elif a.test == "io":
            await t_io(link, events)
        elif a.test == "motors":
            await t_motors(link, a.pct)
        elif a.test == "spin":
            await t_spin(link, a.pct)
        else:
            link.send("stat?")
            await asyncio.sleep(3600)
    finally:
        link.stop()
        link.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
