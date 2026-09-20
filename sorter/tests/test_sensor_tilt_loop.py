"""tests.sensor_tilt_loop with a scripted sensor and two simulated servos.

Run with:
    cd sorter
    python -m pytest tests/test_sensor_tilt_loop.py -v
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
from tests import sensor_tilt_loop as loop
from tests.test_servo_pair import ModalPairWire

ZERO, END_MIN, END_MAX = {43: 2144, 13: 3305}, {43: 2430, 13: 3016}, {43: 1842, 13: 3607}
LEVEL, MIN, MAX = (2144, 3305), (2380, 3066), (1892, 3557)


class ScriptedSensor:
    """read() plays back `script` one sample at a time, then repeats the last value forever."""

    def __init__(self, script):
        self.script = list(script)
        self.reads = 0
        self.closed = False

    def read(self) -> bool:
        value = self.script[min(self.reads, len(self.script) - 1)]
        self.reads += 1
        return value

    def close(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 0.0}
    tick = lambda s: t.__setitem__("now", t["now"] + max(s, 0.001))
    for module in (loop, servo_pair):
        monkeypatch.setattr(module, "_monotonic", lambda: t["now"])
        monkeypatch.setattr(module, "_sleep", tick)
    return t


@pytest.fixture
def run(monkeypatch, clock):
    def _run(sensor, *argv, wire=None, calibrated=True):
        if calibrated:
            Calibration.from_measurements(ZERO, END_MIN, END_MAX).save()
        wire = wire or ModalPairWire({43: 2200, 13: 3250})

        def fake_pair(ids, **kwargs):
            kwargs.pop("mock", None)
            return ServoPair(ids, bus=StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01), mock=False, **kwargs)

        monkeypatch.setattr(loop, "ServoPair", fake_pair)
        monkeypatch.setattr(loop, "MetalSensor", lambda mock=False: sensor)
        auto = [] if "--enter" in argv else ["--auto"]
        return loop.main([a for a in argv if a != "--enter"] + auto + ["--yes"]), wire
    return _run


def moves(wire, sid=43):
    """Commanded goals, minus the hold-where-you-are writes at start-up and shutdown."""
    return wire.goals[sid][1:-1]


# --- wait_for_consistent -------------------------------------------------------------------
def test_consistent_value_is_returned_after_one_full_window(clock):
    sensor = ScriptedSensor([True])
    assert loop.wait_for_consistent(sensor.read, window_s=1.0, sample_s=0.02) is True
    assert clock["now"] == pytest.approx(1.0, abs=0.03)


def test_flicker_restarts_the_window_and_never_decides_early(clock):
    flicker = [True, False] * 40                       # 80 samples = 1.6 s of disagreement
    sensor = ScriptedSensor(flicker + [False])
    assert loop.wait_for_consistent(sensor.read, window_s=1.0, sample_s=0.02) is False
    assert clock["now"] >= 1.6 + 1.0 - 0.05             # a full quiet window AFTER the last flip


def test_a_single_glitch_near_the_end_costs_a_whole_new_window(clock):
    sensor = ScriptedSensor([True] * 45 + [False] + [True])   # glitch at 0.9 s
    assert loop.wait_for_consistent(sensor.read, window_s=1.0, sample_s=0.02) is True
    assert clock["now"] >= 0.9 + 1.0


# --- the loop --------------------------------------------------------------------------------
def test_metal_goes_to_max_and_comes_back_to_level(run, capsys):
    sensor = ScriptedSensor([True])
    code, wire = run(sensor, "--cycles", "2")
    assert code == 0
    assert moves(wire, 43) == [LEVEL[0], MAX[0], LEVEL[0], MAX[0], LEVEL[0]]
    assert moves(wire, 13) == [LEVEL[1], MAX[1], LEVEL[1], MAX[1], LEVEL[1]]
    assert wire.regs[43][40] == 0 and sensor.closed       # torque released, pin released
    assert "2 x battery side, 0 x non-battery side" in capsys.readouterr().out


def test_no_metal_goes_to_min(run):
    code, wire = run(ScriptedSensor([False]), "--cycles", "1")
    assert code == 0
    assert moves(wire, 43) == [LEVEL[0], MIN[0], LEVEL[0]]
    assert moves(wire, 13) == [LEVEL[1], MIN[1], LEVEL[1]]


def test_swap_flips_the_sides(run):
    code, wire = run(ScriptedSensor([True]), "--cycles", "1", "--swap")
    assert moves(wire, 43) == [LEVEL[0], MIN[0], LEVEL[0]]


def test_each_cycle_reads_the_sensor_again(run):
    # metal for the first decision, gone by the second
    sensor = ScriptedSensor([True] * 60 + [False])
    code, wire = run(sensor, "--cycles", "2")
    assert moves(wire, 43) == [LEVEL[0], MAX[0], LEVEL[0], MIN[0], LEVEL[0]]


def test_bed_stays_level_while_the_reading_flickers(run):
    sensor = ScriptedSensor([True, False] * 100 + [True])    # 4 s of flicker, then steady metal
    code, wire = run(sensor, "--cycles", "1")
    assert code == 0
    assert moves(wire, 43) == [LEVEL[0], MAX[0], LEVEL[0]]   # exactly one tilt, only after it settled
    assert sensor.reads > 200


def test_stops_and_levels_if_a_tilt_cannot_be_reached(run, capsys):
    wire = ModalPairWire({43: 2200, 13: 3250}, limits={43: (2000, 2300)})    # 43 cannot get to max (1892)
    code, wire = run(ScriptedSensor([True]), wire=wire)                      # no --cycles: would loop forever
    assert code == 1
    assert "NOT REACHED" in capsys.readouterr().out
    assert wire.word(43, 56) == LEVEL[0]                                      # went back to level before quitting
    assert wire.regs[43][40] == 0


def test_without_calibration_nothing_moves(run, capsys):
    code, wire = run(ScriptedSensor([True]), calibrated=False)
    assert code == 2
    assert wire.goals == {43: [], 13: []}
    assert "Can't use the calibration" in capsys.readouterr().out


# --- Enter gating (the default: no weight sensor yet) ----------------------------------------
def test_default_waits_for_enter_before_every_cycle(run, monkeypatch, capsys):
    sensor = ScriptedSensor([True])
    prompts = []

    def fake_input(prompt=""):
        prompts.append((prompt, sensor.reads))          # how many samples had been taken when we were asked
        return "" if len(prompts) <= 2 else "q"

    monkeypatch.setattr("builtins.input", fake_input)
    code, wire = run(sensor, "--enter")
    assert code == 0
    assert len(prompts) == 3                             # Enter, Enter, q
    assert prompts[0][1] == 0                            # the sensor was not even read before the first Enter
    assert moves(wire, 43) == [LEVEL[0], MAX[0], LEVEL[0], MAX[0], LEVEL[0]]
    assert "2 x battery side, 0 x non-battery side" in capsys.readouterr().out


def test_q_at_the_first_prompt_never_tilts(run, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    code, wire = run(ScriptedSensor([True]), "--enter")
    assert code == 0
    assert moves(wire, 43) == [LEVEL[0]]                 # went level, waited, finished
    assert wire.regs[43][40] == 0


def test_closed_stdin_finishes_cleanly(run, monkeypatch):
    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    code, wire = run(ScriptedSensor([True]), "--enter")
    assert code == 0 and moves(wire, 43) == [LEVEL[0]]
