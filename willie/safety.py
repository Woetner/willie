"""Pi-side safety gate (F1, §10). Every motion command passes `Safety.gate()` first.

The firmware already owns the hard rules that must hold when the Pi hangs (D18): the
200 ms watchdog, the 50 % PWM cap and the bumper/cliff/tilt estop latch. This layer adds
the rules that need the Pi's view of the world, and never loosens the firmware's:

- no fresh `st` from the MCU (> stale_s)          -> refuse
- estop latched in the firmware                    -> refuse (motion.recover() clears it)
- battery under cutoff for battery_hold_s           -> refuse + ask for a safe poweroff
- something closer than tof_stop_mm in front        -> no forward speed (turn/reverse ok)
- closer than 2 x tof_stop_mm                       -> forward speed scaled down (F6)
- speeds clamped to drive.max_speed and max_turn

Pure logic: it reads the last parsed state (hal/link.py) and the settings, and returns
what may be sent. Tests feed it state dicts directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("willie.safety")

STALE_S = 0.25            # 5 missed `st` frames at 50 Hz
BATTERY_HOLD_S = 10.0     # voltage sags under motor load; act on a sustained low only
NO_PACK_MV = 5000        # a 3S pack is never below this; less = no pack connected
MAX_TURN = 2.0            # rad/s: a full turn in ~3 s, gentle for a 1.3 kg robot


@dataclass
class Verdict:
    v: float
    w: float
    reason: str = ""      # "" = allowed as asked; otherwise why it was changed/refused

    @property
    def refused(self) -> bool:
        return self.v == 0 and self.w == 0 and bool(self.reason)


class Safety:
    def __init__(self, get, clock=time.monotonic, on_poweroff=None, on_event=None):
        """`get(dotted)` reads a setting (Config.get). `on_poweroff()` is called once when
        the battery stays under cutoff; `on_event(name, detail)` gets warnings."""
        self.get, self.clock = get, clock
        self.on_poweroff, self.on_event = on_poweroff, on_event
        self._low_since = self._cut_since = None
        self.battery_state = "unknown"        # unknown | ok | low | cutoff
        self._powering_off = False

    # ---- battery ------------------------------------------------------------
    def battery(self, state: dict | None) -> str:
        """Call on every new state. Returns the battery state and fires the callbacks."""
        # < NO_PACK_MV = no pack on the INA219 (bench on USB power): unknown, never "empty".
        if not state or not state.get("ok", {}).get("ina") or state.get("mv", 0) < NO_PACK_MV:
            self.battery_state = "unknown"
            return self.battery_state
        volts, now = state["mv"] / 1000, self.clock()
        warn, cut = self.get("safety.battery_warn_v"), self.get("safety.battery_cutoff_v")
        self._low_since = (now if self._low_since is None else self._low_since) if volts < warn else None
        self._cut_since = (now if self._cut_since is None else self._cut_since) if volts < cut else None
        new = "ok"
        if self._low_since is not None and now - self._low_since >= BATTERY_HOLD_S:
            new = "low"
        if self._cut_since is not None and now - self._cut_since >= BATTERY_HOLD_S:
            new = "cutoff"
        if new != self.battery_state and new in ("low", "cutoff"):
            log.warning("battery %s: %.2f V", new, volts)
            if self.on_event:
                self.on_event("battery." + new, f"{volts:.2f} V")
        self.battery_state = new
        if new == "cutoff" and not self._powering_off and self.on_poweroff:
            self._powering_off = True
            self.on_poweroff()
        return new

    # ---- motion gate ---------------------------------------------------------
    def front_mm(self, state: dict) -> int | None:
        """Nearest reading of the ToF sensors that answered (0 = no reading)."""
        ok = state.get("ok", {})
        seen = [state[f"tof_{s}"] for s in ("l", "c", "r") if ok.get(f"tof_{s}") and state.get(f"tof_{s}", 0) > 0]
        return min(seen) if seen else None

    def gate(self, v: float, w: float, state: dict | None, state_age_s: float) -> Verdict:
        if state is None or state_age_s > STALE_S:
            return Verdict(0.0, 0.0, "geen verbinding met de motorbesturing")
        if state.get("estop"):
            return Verdict(0.0, 0.0, "noodstop: " + ", ".join(state["estop"]))
        if self.battery_state == "cutoff":
            return Verdict(0.0, 0.0, "batterij leeg")
        reason = ""
        vmax = self.get("drive.max_speed")
        if abs(v) > vmax:
            v, reason = max(-vmax, min(vmax, v)), "snelheid begrensd"
        if abs(w) > MAX_TURN:
            w, reason = max(-MAX_TURN, min(MAX_TURN, w)), "draaisnelheid begrensd"
        front, stop = self.front_mm(state), self.get("safety.tof_stop_mm")
        if v > 0 and front is not None:
            if front <= stop:
                v, reason = 0.0, f"obstakel op {front} mm"
            elif front < 2 * stop:
                v, reason = v * (front - stop) / stop, f"afremmen, obstakel op {front} mm"
        return Verdict(v, w, reason)
