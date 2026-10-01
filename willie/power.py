"""Battery percentage, power draw and runtime from the pack voltage and current the MCU reports.

The numbers come from the INA219 (`mv`, `ma` in the `st` line). Percentage is an estimate from the
cell voltage (typical NMC curve, Molicel P28A), corrected for the voltage sag under load. It is
0 % at the safe-poweroff voltage, not at an empty cell, so runtime means "time until WILL-E shuts
himself down". Pure functions + one small class; no I/O (D21: no dependencies, no RAM to speak of).
"""
from __future__ import annotations

# Resting cell voltage -> % of full charge, typical NMC 18650. An estimate, not a fuel gauge.
_OCV = [(3.00, 0.0), (3.30, 4.0), (3.50, 18.0), (3.60, 32.0), (3.70, 48.0),
        (3.80, 62.0), (3.90, 73.0), (4.00, 83.0), (4.10, 92.0), (4.20, 100.0)]


def _soc(v_cell: float) -> float:
    if v_cell <= _OCV[0][0]:
        return 0.0
    if v_cell >= _OCV[-1][0]:
        return 100.0
    for (v0, p0), (v1, p1) in zip(_OCV, _OCV[1:]):
        if v0 <= v_cell <= v1:
            return p0 + (p1 - p0) * (v_cell - v0) / (v1 - v0)
    return 0.0


def usable_percent(v_cell_rest: float, v_cell_cutoff: float) -> float:
    """% of the charge between the cutoff voltage and full (0 at cutoff, 100 at 4.2 V per cell)."""
    low = _soc(v_cell_cutoff)
    return max(0.0, min(100.0, (_soc(v_cell_rest) - low) / (100.0 - low) * 100.0))


class PowerEstimator:
    """Call `update(mv, ma, now)` every state tick (about every 2 s)."""

    def __init__(self, cells: int = 3, pack_wh: float = 28.0, r_int_ohm: float = 0.15,
                 cutoff_v: float = 9.9, power_tau_s: float = 60.0, volt_tau_s: float = 20.0):
        self.cells, self.pack_wh, self.r_int = cells, pack_wh, r_int_ohm
        self.cutoff_v, self.power_tau, self.volt_tau = cutoff_v, power_tau_s, volt_tau_s
        self._t = self._p = self._v = None
        self.used_wh = 0.0                    # energy drawn since the core started

    def update(self, mv, ma, now: float) -> dict:
        empty = {"battery_pct": None, "power_w": None, "runtime_min": None, "used_wh": round(self.used_wh, 2)}
        if not mv or mv < 5000:               # no pack (bench on USB): the INA219 sees ~0 V
            self._t = self._p = self._v = None
            return empty
        volts, amps = mv / 1000.0, max(0.0, (ma or 0) / 1000.0)
        power = volts * amps
        rest_v = volts + amps * self.r_int    # the voltage the pack would have without the load
        if self._t is None:
            self._p, self._v = power, rest_v
        else:
            dt = max(0.0, now - self._t)
            self.used_wh += power * dt / 3600.0
            ka, kv = min(1.0, dt / self.power_tau), min(1.0, dt / self.volt_tau)
            self._p += (power - self._p) * ka
            self._v += (rest_v - self._v) * kv
        self._t = now
        pct = usable_percent(self._v / self.cells, self.cutoff_v / self.cells)
        runtime = None
        if self._p >= 0.5:                    # below half a watt the number means nothing
            runtime = int(round(self.pack_wh * pct / 100.0 / self._p * 60.0))
        return {"battery_pct": int(round(pct)), "power_w": round(power, 1),
                "runtime_min": runtime, "used_wh": round(self.used_wh, 2)}
