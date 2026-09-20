"""tests.run_pair against two simulated servos that really turn in wheel mode.

Run with:
    cd sorter
    python -m pytest tests/test_run_pair.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators import servo_pair
from actuators.sts_bus import StsBus
from tests import run_pair
from tests.test_servo_pair import ModalPairWire

POLL_S = 0.05


class SpinningPairWire(ModalPairWire):
    """In wheel mode, every position read advances the servo by speed x one poll
    interval - unless that servo is in `blocked`."""

    def __init__(self, positions, blocked=()):
        super().__init__(positions)
        self.blocked = set(blocked)

    def write(self, data):
        data = bytes(data)
        sid = data[2]
        if sid in self.regs and data[4] == 0x02 and data[5] == 56 and self.regs[sid][33] == 1 and sid not in self.blocked:
            speed = self.spins[sid][-1] if self.spins[sid] else 0
            self._set(sid, 56, (self.word(sid, 56) + round(speed * POLL_S)) % 4096)
        return super().write(data)


@pytest.fixture
def run(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(servo_pair, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(servo_pair, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + POLL_S))
    monkeypatch.setattr(run_pair.time, "sleep", lambda s: None)

    def _run(wire, *argv):
        def fake_pair(ids, **kwargs):
            kwargs.pop("mock", None)
            bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
            return servo_pair.ServoPair(ids, bus=bus, mock=False, **kwargs)
        monkeypatch.setattr(run_pair, "ServoPair", fake_pair)
        return run_pair.main([*argv, "--yes"])
    return _run


def test_runs_forward_then_back_and_passes(run, capsys):
    wire = SpinningPairWire({43: 2053, 13: 2840})
    assert run(wire) == 0
    assert wire.spins[43] == [200, 0, -200, 0]              # forward, stop, back, stop
    assert wire.spins[13] == [200, 0, -200, 0]
    assert wire.regs[43][33] == 0 and wire.regs[13][33] == 0  # left in position mode
    assert abs(wire.word(43, 56) - 2053) <= 20               # ended up roughly where it started
    assert "SAME raw direction" in capsys.readouterr().out


def test_invert_13_spins_follower_the_other_way(run, capsys):
    wire = SpinningPairWire({43: 2053, 13: 2840})
    assert run(wire, "--invert-13", "--one-way") == 0
    assert wire.spins[43] == [200, 0]
    assert wire.spins[13] == [-200, 0]
    assert "OPPOSITE raw direction" in capsys.readouterr().out


def test_reverse_and_speed_and_seconds(run):
    wire = SpinningPairWire({43: 2053, 13: 2840})
    assert run(wire, "--reverse", "--speed", "400", "--seconds", "2", "--one-way") == 0
    assert wire.spins[43][0] == -400
    assert abs((2053 - wire.word(43, 56)) % 4096 - 800) <= 40   # ~ speed x seconds, going down


def test_travel_is_counted_across_the_4095_wrap(run):
    wire = SpinningPairWire({43: 4000, 13: 4050})                # will wrap past 0
    assert run(wire, "--speed", "400", "--one-way") == 0


def test_fails_when_one_servo_is_blocked(run, capsys):
    wire = SpinningPairWire({43: 2053, 13: 2840}, blocked={13})
    assert run(wire, "--one-way") == 1
    assert "barely turned" in capsys.readouterr().out
    assert wire.spins[13][-1] == 0                               # still stopped cleanly


def test_refuses_long_runs(run):
    wire = SpinningPairWire({43: 2053, 13: 2840})
    assert run(wire, "--seconds", "60") == 2
    assert wire.spins == {43: [], 13: []}


def test_missing_servo(run):
    assert run(SpinningPairWire({43: 2053})) == 1
