"""tests.read_positions against two simulated servos being "moved by hand".

Run with:
    cd sorter
    python -m pytest tests/test_read_positions.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators.calibration import Calibration
from actuators.sts_bus import StsBus
from tests import read_positions
from tests.read_positions import Recorder, relative
from tests.test_move_pair import PairWire


@pytest.fixture
def run(monkeypatch):
    def _run(wire, script, *argv):
        """script: what happens at each prompt - a dict of new hand positions, or a typed line."""
        steps = iter(script)

        def fake_read_line(timeout_s):
            step = next(steps, "q")
            if isinstance(step, dict):          # someone moved the mechanism; nothing typed
                for sid, pos in step.items():
                    wire._set(sid, 56, pos)
                return None
            return step

        monkeypatch.setattr(read_positions, "_read_line", fake_read_line)
        monkeypatch.setattr(read_positions, "StsBus", lambda port, baud: StsBus(port, serial_port=wire, echo=True, reply_timeout_s=0.01))
        return read_positions.main([*argv, "--yes"])
    return _run


def test_records_named_positions_and_travel(run, capsys):
    wire = PairWire({43: 2140, 13: 3307})
    wire.regs[43][40] = wire.regs[13][40] = 1                       # torque was on
    script = [{43: 1800, 13: 3600}, "min", {43: 2140, 13: 3307}, "level", {43: 2500, 13: 3000}, "max", "q"]
    assert run(wire, script) == 0
    assert wire.regs[43][40] == 0 and wire.regs[13][40] == 0        # released so it can be moved by hand
    assert wire.goals == {43: [], 13: []}                           # never commanded anywhere

    out = capsys.readouterr().out
    table = out[out.index("Send this back"):]
    assert "min" in table and "level" in table and "max" in table
    assert "servo 43: 1800 .. 2500   (travel 700 ticks" in table
    assert "servo 13: 3000 .. 3600   (travel 600 ticks" in table
    assert "!!" not in table


def test_keep_torque_leaves_torque_alone(run):
    wire = PairWire({43: 2140, 13: 3307})
    wire.regs[43][40] = wire.regs[13][40] = 1
    assert run(wire, ["q"], "--keep-torque") == 0
    assert wire.regs[43][40] == 1 and wire.regs[13][40] == 1


def test_unnamed_snapshots_are_numbered(run, capsys):
    wire = PairWire({43: 2140, 13: 3307})
    assert run(wire, ["", "", "q"]) == 0
    out = capsys.readouterr().out
    assert "'#1'" in out and "'#2'" in out


def test_warns_when_travel_crosses_the_wrap(run, capsys):
    wire = PairWire({43: 4000, 13: 3307})
    assert run(wire, [{43: 4090}, {43: 30}, "q"]) == 0              # 4090 -> 30 is a step across 4095/0
    out = capsys.readouterr().out
    assert "!! servo 43 crossed the 4095 <-> 0 boundary" in out
    assert "!! servo 13" not in out


def test_custom_ids_and_missing_servo(run, capsys):
    assert run(PairWire({7: 100}), ["q"], "7") == 0
    assert run(PairWire({43: 2140}), ["q"]) == 1                     # 13 is not there
    assert "[13] did not answer" in capsys.readouterr().out


def test_recorder_tracks_extremes_without_any_snapshot():
    rec = Recorder([1])
    for pos in (2000, 1500, 2600, 2100):
        rec.poll({1: pos})
    assert (rec.low[1], rec.high[1]) == (1500, 2600)
    assert "(no named positions recorded)" in rec.summary()


def test_zero_makes_readings_relative(run, capsys):
    wire = PairWire({43: 2107, 13: 3343})
    script = ["zero", {43: 1807, 13: 3643}, "min", {43: 2407, 13: 3043}, "max", "q"]
    assert run(wire, script) == 0
    out = capsys.readouterr().out
    assert "zero set: servo 43 = 2107, servo 13 = 3343" in out
    table = out[out.index("Send this back"):]
    assert "1807 ( -300)" in table and "3643 ( +300)" in table      # min
    assert "2407 ( +300)" in table and "3043 ( -300)" in table      # max
    assert "ABSOLUTE ticks" in table


def test_relative_is_correct_across_the_wrap():
    assert relative(30, 4090) == 36          # 4090 -> 30 is +36 ticks, not -4060
    assert relative(4090, 30) == -36
    assert relative(2048, 2048) == 0
    assert relative(1000, 3000) == -2000


def test_save_writes_the_global_calibration(run, capsys, isolated_calibration):
    wire = PairWire({43: 1786, 13: 3660})
    script = ["zero", {43: 1490, 13: 3955}, "min", {43: 2080, 13: 3365}, "max", "save", "q"]
    assert run(wire, script) == 0
    assert "saved ->" in capsys.readouterr().out
    cal = Calibration.load()
    assert (cal[43].zero, cal[43].lower, cal[43].upper, cal[43].invert) == (1786, 1490, 2080, False)
    assert (cal[13].zero, cal[13].lower, cal[13].upper, cal[13].invert) == (3660, 3365, 3955, True)


def test_save_needs_a_zero_and_refuses_wrapped_travel(run, capsys, isolated_calibration):
    assert run(PairWire({43: 1786, 13: 3660}), ["save", "q"]) == 0
    assert "NOT saved: no zero yet" in capsys.readouterr().out
    assert not isolated_calibration.exists()

    assert run(PairWire({43: 4000, 13: 3660}), ["zero", {43: 4090}, {43: 30}, "max", "save", "q"]) == 0
    assert "NOT saved" in capsys.readouterr().out
    assert not isolated_calibration.exists()
