"""actuators.calibration - the globally stored servo zero / end stops / mirroring.

Run with:
    cd sorter
    python -m pytest tests/test_calibration.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators.calibration import Calibration, ServoCal
from actuators.sts_bus import StsBus
from tests.test_move_pair import PairWire

# The rig as measured: 43 turns up toward "max", 13 faces it and turns down.
ZERO = {43: 1786, 13: 3660}
END_MIN = {43: 1490, 13: 3955}
END_MAX = {43: 2080, 13: 3365}


def rig() -> Calibration:
    return Calibration.from_measurements(ZERO, END_MIN, END_MAX)


def test_from_measurements_sorts_end_stops_and_detects_mirroring():
    cal = rig()
    assert cal[43] == ServoCal(zero=1786, lower=1490, upper=2080, invert=False)
    assert cal[13] == ServoCal(zero=3660, lower=3365, upper=3955, invert=True)
    assert cal.problems() == []


def test_same_mechanism_move_maps_to_opposite_servo_ticks():
    cal = rig()
    assert cal[43].to_abs(+200) == 1986
    assert cal[13].to_abs(+200) == 3460           # mirrored: ticks go down
    assert cal[43].to_rel(1986) == 200 and cal[13].to_rel(3460) == 200
    assert cal[43].to_abs(0) == 1786 and cal[13].to_abs(0) == 3660
    assert cal[43].rel_range() == (-296, 294) and cal[13].rel_range() == (-295, 295)   # raw recorded travel


def test_relative_targets_are_held_inside_the_safe_travel():
    cal = rig()
    assert cal[43].to_abs(+5000) == 2080 - 50         # default: setpoint margin inside the recorded end
    assert cal[13].to_abs(+5000) == 3365 + 50
    assert cal[43].to_abs(-5000) == 1490 + 50
    assert cal[43].to_abs(+5000, margin=0) == 2080    # raw recorded end, only if asked for
    assert cal[43].to_abs(+5000) == cal[43].setpoint("max")   # move_rel can never pass go_max()


def test_save_and_load_round_trip(isolated_calibration):
    path = rig().save()
    assert path == isolated_calibration
    assert json.loads(path.read_text())["servos"]["13"]["invert"] is True
    loaded = Calibration.load()
    assert loaded.servos == rig().servos


def test_missing_file_is_an_empty_calibration_with_a_helpful_error():
    cal = Calibration.load()
    assert cal.servos == {} and 43 not in cal
    with pytest.raises(KeyError, match="read_positions"):
        cal[43]


def test_zero_only_calibration_has_no_bounds():
    cal = Calibration.from_measurements({43: 1786})
    assert cal[43].to_abs(+300) == 2086
    assert cal[43].bounds() == (0, 4095)


def test_flags_travel_that_crosses_the_wrap():
    cal = Calibration.from_measurements({13: 4000}, {13: 3800}, {13: 100})   # "max" wrapped past 4095
    assert any("4095" in p for p in cal.problems())
    with pytest.raises(ValueError):
        cal.apply_limits(bus=None)


def test_apply_limits_writes_travel_minus_margin_to_each_servo():
    wire = PairWire(dict(ZERO))
    bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
    written = rig().apply_limits(bus)                 # default margin: config.SERVO_LIMIT_MARGIN = 25
    assert written == {13: (3390, 3930), 43: (1515, 2055)}
    assert (wire.word(43, 9), wire.word(43, 11)) == (1515, 2055)
    assert (wire.word(13, 9), wire.word(13, 11)) == (3390, 3930)
    assert wire.regs[43][55] == 1 and wire.regs[13][55] == 1      # EEPROM locked again


def test_setpoints_sit_further_in_than_the_servo_limits():
    cal = rig()
    for sid in (43, 13):
        lower, upper = cal[sid].bounds(25)             # what apply_limits writes
        for name in ("min", "max"):
            sp = cal[sid].setpoint(name)
            assert lower + 25 <= sp <= upper - 25      # 25 ticks of overshoot room before the limit


def test_setpoint_margin_can_never_be_smaller_than_the_limit_margin(monkeypatch):
    import config
    cal = rig()
    assert cal[43].setpoint("max", margin=5) == 2080 - 25      # asked for 5, held at the limit margin
    monkeypatch.setattr(config, "SERVO_SETPOINT_MARGIN", 10)   # misconfigured: smaller than the limit margin
    assert cal[43].setpoint("max") == 2080 - 25
    monkeypatch.setattr(config, "SERVO_SETPOINT_MARGIN", 120)
    assert cal[43].setpoint("max") == 2080 - 120
