"""tools.nudge against a simulated servo that actually "moves" (and clamps goals
to its stored angle limits, like the real one does).

Run with:
    cd sorter
    python -m pytest tests/test_nudge.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators.sts_bus import StsBus
from tests.test_sts_bus import FakeWire
from tools import nudge


class MovingServoWire(FakeWire):
    def __init__(self, position, limits=(0, 4095), **kwargs):
        super().__init__(**kwargs)
        self._set_word(56, position)
        self._set_word(9, limits[0])
        self._set_word(11, limits[1])
        self.goals = []

    def _set_word(self, address, value):
        self.registers[address:address + 2] = value.to_bytes(2, "little")

    def _word(self, address):
        return int.from_bytes(self.registers[address:address + 2], "little")

    def _servo(self, packet):
        reply = super()._servo(packet)
        if reply and packet[4] == 0x03 and packet[5] == 41:  # write_pos_ex: jump straight to the (clamped) goal
            goal = self._word(42)
            lower, upper = self._word(9), self._word(11)
            if not (lower == 0 and upper == 0):
                goal = min(max(goal, lower), upper)
            self.goals.append(goal)
            self._set_word(56, goal)
        return reply


@pytest.fixture
def run(monkeypatch):
    def _run(wire, *argv):
        monkeypatch.setattr(nudge, "StsBus", lambda port, baud: StsBus(port, serial_port=wire, echo=True, reply_timeout_s=0.01))
        return nudge.main([*argv, "--yes"])
    return _run


def test_nudge_moves_and_returns(run, capsys):
    wire = MovingServoWire(position=2000, servo_id=43)
    assert run(wire, "43") == 0
    assert wire.goals == [2100, 2000]
    assert wire.registers[40] == 0                        # torque released at the end
    assert int.from_bytes(wire.registers[48:50], "little") == 300  # gentle torque limit was set
    assert "Reached target" in capsys.readouterr().out


def test_refuses_when_limits_would_turn_nudge_into_swing(run, capsys):
    # The real servo on the bench: at 2076 with another robot's limits still in EEPROM.
    wire = MovingServoWire(position=2076, limits=(2863, 3459), servo_id=43)
    assert run(wire, "43") == 2
    assert wire.goals == []                               # nothing moved
    assert wire.registers[40] == 0                        # torque never enabled
    assert "787 ticks" in capsys.readouterr().out         # says how far it WOULD have swung


def test_open_limits_then_moves(run):
    wire = MovingServoWire(position=2076, limits=(2863, 3459), servo_id=43)
    assert run(wire, "43", "--open-limits") == 0
    assert (wire._word(9), wire._word(11)) == (0, 4095)
    assert wire.registers[55] == 1                        # EEPROM locked again
    assert wire.goals == [2176, 2076]


def test_stay_and_negative_ticks(run):
    wire = MovingServoWire(position=2000, servo_id=43)
    assert run(wire, "43", "--ticks", "-150", "--stay") == 0
    assert wire.goals == [1850]


def test_refuses_big_moves_and_wrong_mode(run):
    wire = MovingServoWire(position=2000, servo_id=43)
    assert run(wire, "43", "--ticks", "900") == 2
    wire.registers[33] = 1                                # wheel mode
    assert run(wire, "43") == 2
    assert wire.goals == []


def test_silent_servo(run):
    assert run(MovingServoWire(position=2000, servo_id=None), "43") == 1
