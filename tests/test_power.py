"""Battery percentage / power / runtime estimate (willie/power.py)."""
from willie.power import PowerEstimator, usable_percent


def test_percent_endpoints_and_order():
    assert usable_percent(4.2, 3.3) == 100.0
    assert usable_percent(3.3, 3.3) == 0.0
    assert usable_percent(3.0, 3.3) == 0.0
    last = -1.0
    for mv in range(3300, 4201, 50):
        pct = usable_percent(mv / 1000, 3.3)
        assert pct >= last
        last = pct


def test_no_pack_gives_nothing():
    est = PowerEstimator()
    out = est.update(0, 0, 0.0)
    assert out["battery_pct"] is None and out["power_w"] is None and out["runtime_min"] is None
    assert est.update(3000, 100, 2.0)["battery_pct"] is None      # bench: INA219 sees USB-level junk


def test_idle_numbers():
    est = PowerEstimator(pack_wh=28.0)
    out = est.update(12100, 125, 0.0)                              # what the robot reported idle
    assert out["power_w"] == 1.5
    assert 80 <= out["battery_pct"] <= 92
    assert 700 <= out["runtime_min"] <= 1100                       # roughly 12-18 h at 1.5 W


def test_sag_correction_raises_percent_under_load():
    light, heavy = PowerEstimator(), PowerEstimator()
    a = light.update(11800, 100, 0.0)["battery_pct"]
    b = heavy.update(11800, 2000, 0.0)["battery_pct"]              # same voltage at 2 A: pack is fuller
    assert b > a


def test_power_is_smoothed_and_energy_counts():
    est = PowerEstimator(power_tau_s=60)
    est.update(12000, 100, 0.0)                                    # 1.2 W
    out = est.update(12000, 1000, 2.0)                             # 12 W spike for 2 s
    assert out["power_w"] == 12.0                                  # shown now
    assert out["runtime_min"] > 100                                # but runtime uses the smoothed power
    assert out["used_wh"] > 0


def test_tiny_load_has_no_runtime():
    est = PowerEstimator()
    assert est.update(12000, 10, 0.0)["runtime_min"] is None       # 0.12 W: meaningless


def test_jump_of_30_points_means_charging_and_back_means_unplugged():
    est = PowerEstimator()
    for t in range(0, 60, 2):
        assert est.update(11300, 300, float(t))["charging"] is False   # settled around 30 %
    out = est.update(12500, 300, 60.0)                                 # charger lifts the pack
    assert out["charging"] is True
    for t in range(62, 200, 2):                                        # smoothing catches up, stays latched
        out = est.update(12500, 300, float(t))
    assert out["charging"] is True
    assert est.update(11300, 300, 200.0)["charging"] is False          # unplugged: big drop


def test_slow_drift_is_not_charging_and_no_pack_clears_it():
    est = PowerEstimator()
    for i, t in enumerate(range(0, 400, 2)):
        assert est.update(11000 + i * 5, 300, float(t))["charging"] is False
    est.update(12500, 300, 400.0)
    assert est.update(0, 0, 402.0)["charging"] is False
