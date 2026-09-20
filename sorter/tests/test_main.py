"""main.py's weight trigger: a RISE of more than WEIGHT_DELTA_TRIGGER_G above the resting level starts a scan.

Run with:
    cd sorter
    python -m pytest tests/test_main.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import main
from main import State


class ScriptedScale:
    """read_weight_g() plays back `weights` in order, then repeats the last one."""
    mock = False

    def __init__(self, weights):
        self.weights = list(weights)
        self.reads = 0
        self.tares = 0

    def read_weight_g(self, samples=1):
        value = self.weights[min(self.reads, len(self.weights) - 1)]
        self.reads += 1
        return value

    def stable_reading(self):
        return self.weights[min(self.reads, len(self.weights) - 1)]

    def tare(self):
        self.tares += 1

    def close(self):
        pass


@pytest.fixture
def machine_factory(monkeypatch):
    monkeypatch.setattr(config, "MOCK_HARDWARE", False)       # exercise the real-hardware code path...
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    moves = []

    class FakePair:
        def __init__(self, ids, **kwargs): pass
        def go_level(self, **kwargs): moves.append("level"); return True
        def go_to(self, name, **kwargs): moves.append(name); return True
        def close(self): pass

    class FakeMetal:
        def is_metal_present(self): return False
        def close(self): pass

    monkeypatch.setattr(main, "ServoPair", FakePair)          # ...with every device faked
    monkeypatch.setattr(main, "MetalSensor", FakeMetal)
    monkeypatch.setattr(main, "capture_frame", lambda: b"jpeg")
    monkeypatch.setattr(main, "classify_material", lambda frame: {"plastic": True})

    def make(weights):
        scale = ScriptedScale(weights)
        monkeypatch.setattr(main, "LoadCell", lambda: scale)
        return main.SorterStateMachine(), scale, moves
    return make


def rest(value):
    """The three agreeing readings that establish the resting weight on entering WAITING."""
    return [value] * config.WEIGHT_REST_SAMPLES


def wait_steps(machine, n):
    for _ in range(n):
        assert machine.state is State.WAITING
        machine.step()


T = config.WEIGHT_DELTA_TRIGGER_G     # 10 g


def test_a_rise_of_more_than_the_threshold_starts_the_scan(machine_factory):
    machine, scale, moves = machine_factory(rest(500.0) + [500.0, 500.4, 530.0])   # resting 500, then +30
    wait_steps(machine, 2)
    assert machine.state is State.WAITING
    machine.step()
    assert machine.state is State.SENSING
    machine.step()
    assert machine.weight_g == pytest.approx(30.0, abs=0.5)                 # the item's own weight, not 530


def test_the_absolute_reading_does_not_matter(machine_factory):
    # A scale whose zero is wildly off: resting at -1490 g. Just over the threshold still triggers, just under does not.
    machine, _, _ = machine_factory(rest(-1490.0) + [-1490.0 + T - 1, -1490.0 + T + 1])
    machine.step()
    assert machine.state is State.WAITING                                    # threshold - 1 g: not enough
    machine.step()
    assert machine.state is State.SENSING                                    # threshold + 1 g


def test_exactly_the_threshold_is_not_more_than_it(machine_factory):
    machine, _, _ = machine_factory(rest(100.0) + [100.0 + T, 100.0 + T, 100.0 + T + 0.1])
    wait_steps(machine, 2)
    assert machine.state is State.WAITING
    machine.step()
    assert machine.state is State.SENSING


def test_slow_drift_is_followed_and_never_triggers(machine_factory):
    drifting = [100.0 + 0.05 * i for i in range(2000)]                       # +100 g in total, creeping 0.05 g a poll
    machine, _, _ = machine_factory(drifting)
    wait_steps(machine, 1999)
    assert machine.state is State.WAITING
    assert machine._baseline_g > 195.0                                       # the resting level went with it


def test_an_item_lowered_slowly_still_triggers(machine_factory):
    lowering = [100.0 + 2.0 * i for i in range(40)]                          # 2 g a poll: far faster than any drift
    machine, _, _ = machine_factory(lowering)
    for _ in range(40):
        if machine.state is not State.WAITING:
            break
        machine.step()
    assert machine.state is State.SENSING


def test_taking_something_off_resets_the_resting_level(machine_factory):
    machine, _, _ = machine_factory(rest(500.0) + [500.0, 200.0, 200.0, 200.0 + T + 3])
    wait_steps(machine, 3)                                                   # 500 -> 200: a drop, not a trigger
    assert machine._baseline_g == pytest.approx(200.0)
    machine.step()
    assert machine.state is State.SENSING                                    # threshold + 3 g above the NEW level


def test_full_cycle_then_a_fresh_resting_level(machine_factory):
    machine, scale, moves = machine_factory(rest(0.0) + [0.0, 40.0, 40.0] + rest(3.0) + [3.0, 45.0])
    machine.run(max_cycles=1)
    assert moves == ["level", "non_battery", "level"]
    assert machine.state is State.WAITING and machine._baseline_g is None    # will be re-measured
    assert scale.tares == 1                                                  # start-up only: no re-tare per cycle any more
    machine.step()
    assert machine._baseline_g == pytest.approx(3.0)                         # residue left on the bed is the new normal


def test_mock_mode_still_fires_every_loop(monkeypatch):
    from actuators.calibration import Calibration
    Calibration.from_measurements({43: 2144, 13: 3305}, {43: 2430, 13: 3016}, {43: 1842, 13: 3607}).save()
    monkeypatch.setattr(config, "MOCK_HARDWARE", True)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    machine = main.SorterStateMachine()
    machine.run(max_cycles=2)
    assert machine.cycles_done == 2
    machine.close()


def test_resting_weight_waits_for_a_bouncing_bed_to_settle(machine_factory):
    # Right after a dump the bed is still bouncing: 40, -25, 18, -6 ... then it settles near 2 g.
    machine, scale, _ = machine_factory([40.0, -25.0, 18.0, -6.0, 2.2, 1.9, 2.1, 2.0, 2.0])
    machine.step()
    assert machine._baseline_g == pytest.approx(2.07, abs=0.1)               # not 40, not the average of the bounce
    assert machine.state is State.WAITING                                     # and the bounce itself did not trigger a scan


def test_a_settled_bed_is_accepted_after_three_readings(machine_factory):
    machine, scale, _ = machine_factory([5.0, 5.1, 4.9, 5.0, 5.0])
    machine.step()
    assert scale.reads == 3 + 1                                               # 3 to establish rest, then the first poll
