"""Setpoints (level / min / max) with the numbers actually measured on the rig.

Run with:
    cd sorter
    python -m pytest tests/test_goto_setpoints.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators import servo_pair
from actuators.calibration import Calibration
from actuators.servo_pair import ServoPair
from actuators.sts_bus import StsBus
from tests import goto_setpoints
from tests.test_servo_pair import ModalPairWire

# Measured 2026-09-19 with read_positions.py: 43 is mirrored, 13 is not.
ZERO = {43: 2144, 13: 3305}
END_MIN = {43: 2430, 13: 3016}
END_MAX = {43: 1842, 13: 3607}


def rig() -> Calibration:
    return Calibration.from_measurements(ZERO, END_MIN, END_MAX)


def make(positions=None, limits=None):
    wire = ModalPairWire(positions or dict(ZERO), limits)
    bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
    return ServoPair((43, 13), bus=bus, mock=False, calibration=rig()), wire


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(servo_pair, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(servo_pair, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + 0.05))
    monkeypatch.setattr(goto_setpoints.time, "sleep", lambda s: None)


def test_calibration_matches_what_the_rig_reported():
    cal = rig()
    assert cal[43].invert is True and cal[13].invert is False
    assert (cal[43].lower, cal[43].upper) == (1842, 2430)
    assert (cal[13].lower, cal[13].upper) == (3016, 3607)


def test_setpoints_are_each_servos_own_end_50_ticks_inside():
    cal = rig()
    assert (cal[43].setpoint("max"), cal[13].setpoint("max")) == (1892, 3557)   # recorded 1842 / 3607
    assert (cal[43].setpoint("min"), cal[13].setpoint("min")) == (2380, 3066)   # recorded 2430 / 3016
    assert (cal[43].setpoint("level"), cal[13].setpoint("level")) == (2144, 3305)
    with pytest.raises(ValueError):
        cal[43].setpoint("sideways")


def test_go_max_and_go_min_turn_the_servos_opposite_ways():
    pair, wire = make()
    assert pair.go_max() is True
    assert pair.positions() == (1892, 3557)          # 43 ticks DOWN, 13 ticks UP: same way for the bed
    assert pair.positions_rel() == (252, 252)
    assert pair.go_min() is True
    assert pair.positions() == (2380, 3066)
    assert pair.positions_rel() == (-236, -239)
    assert pair.go_level() is True
    assert pair.positions() == (2144, 3305)


def test_setpoints_are_reachable_once_limits_are_written_to_the_servos():
    # The servo clamps goals to its EEPROM limits; setpoints must sit exactly on them, not past.
    pair, wire = make()
    rig().apply_limits(pair._bus)
    assert pair.go_max() is True and pair.positions() == (1892, 3557)
    assert pair.go_min() is True and pair.positions() == (2380, 3066)
    assert pair._bus.angle_limits(43) == (1867, 2405)         # limits are 25 ticks further out than the setpoints


def test_both_servos_start_in_one_packet():
    pair, wire = make()
    before = len(wire.sync_packets)
    pair.go_max()
    assert len(wire.sync_packets) == before + 1


def test_end_stops_not_measured_gives_a_clear_error():
    cal = Calibration.from_measurements(ZERO)        # zero only
    wire = ModalPairWire(dict(ZERO))
    bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
    pair = ServoPair((43, 13), bus=bus, mock=False, calibration=cal)
    assert pair.go_level() is True
    with pytest.raises(ValueError, match="end stops not measured"):
        pair.go_max()


# --- the physical script, against the simulated rig -----------------------------------------
@pytest.fixture
def run(monkeypatch):
    def _run(wire, *argv):
        rig().save()                                 # into the isolated temp calibration file
        def fake_pair(ids, **kwargs):
            kwargs.pop("mock", None)
            bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
            return ServoPair(ids, bus=bus, mock=False, **kwargs)
        monkeypatch.setattr(goto_setpoints, "ServoPair", fake_pair)
        return goto_setpoints.main([*argv, "--yes"])
    return _run


def test_script_walks_the_default_sequence(run, capsys):
    wire = ModalPairWire({43: 2300, 13: 3200})
    assert run(wire) == 0
    # [0] = hold-where-you-are at startup, [1:6] = level -> min -> level -> max -> level,
    # [6] = hold-where-you-are again as close() hands back to position mode before releasing.
    assert wire.goals[43][1:6] == [2144, 2380, 2144, 1892, 2144]
    assert wire.goals[13][1:6] == [3305, 3066, 3305, 3557, 3305]
    assert wire.goals[43][6:] == [2144] and wire.goals[13][6:] == [3305]   # ...and that hold moved nothing
    assert wire.regs[43][40] == 0 and wire.regs[13][40] == 0       # torque released at the end
    assert "OK: every setpoint reached" in capsys.readouterr().out


def test_script_single_setpoint_and_hold(run):
    wire = ModalPairWire({43: 2300, 13: 3200})
    assert run(wire, "max", "--hold") == 0
    assert (wire.word(43, 56), wire.word(13, 56)) == (1892, 3557)
    assert wire.regs[43][40] == 1                                  # still holding


def test_script_stops_at_the_first_setpoint_it_cannot_reach(run, capsys):
    wire = ModalPairWire({43: 2300, 13: 3200}, limits={43: (2000, 2300)})   # 43 can't get to min (2380)
    assert run(wire, "level", "min", "max") == 1
    out = capsys.readouterr().out
    assert "NOT REACHED" in out and "did not reach 'min'" in out
    assert 1892 not in wire.goals[43]                              # never went on to max


def test_script_without_calibration(monkeypatch, capsys):
    assert goto_setpoints.main(["--yes"]) == 2
    assert "Can't build the plan" in capsys.readouterr().out


def test_script_margin_option_stops_further_short(run):
    wire = ModalPairWire({43: 2300, 13: 3200})
    assert run(wire, "max", "--margin", "100", "--hold") == 0
    assert (wire.word(43, 56), wire.word(13, 56)) == (1942, 3507)   # recorded 1842 / 3607, 100 ticks short
