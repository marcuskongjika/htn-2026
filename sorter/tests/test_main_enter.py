"""main_enter.py: the main.py pipeline started by Enter, with the load cell never touched.

Run with:
    cd sorter
    python -m pytest tests/test_main_enter.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import main_enter
from main import State


@pytest.fixture
def machine_factory(monkeypatch):
    monkeypatch.setattr(config, "MOCK_HARDWARE", True)
    monkeypatch.setattr(main_enter.time, "sleep", lambda s: None)      # TILT_HOLD_S / RESETTING_PAUSE_S

    # If anything in this variant ever opens the load cell, fail loudly.
    import sensors.load_cell as load_cell_module

    def forbidden(*args, **kwargs):
        raise AssertionError("main_enter must never construct a LoadCell")
    monkeypatch.setattr(load_cell_module, "LoadCell", forbidden)

    moves = []

    class FakePair:
        def __init__(self, ids, **kwargs):
            self.ids = ids
        def go_level(self, **kwargs):
            moves.append("level"); return True
        def go_to(self, name, **kwargs):
            moves.append(name); return True
        def close(self):
            moves.append("closed")

    class FakeMetal:
        def __init__(self): self.value = False
        def is_metal_present(self): return self.value
        def close(self): moves.append("metal closed")

    metal = FakeMetal()
    classification = {"plastic": True}
    monkeypatch.setattr(main_enter, "ServoPair", FakePair)
    monkeypatch.setattr(main_enter, "MetalSensor", lambda: metal)
    monkeypatch.setattr(main_enter, "capture_frame", lambda: b"jpeg")
    monkeypatch.setattr(main_enter, "classify_material", lambda frame: dict(classification))

    def make(inputs=(), **kwargs):
        answers = iter(inputs)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            try:
                return next(answers)
            except StopIteration:
                raise EOFError
        monkeypatch.setattr("builtins.input", fake_input)
        return main_enter.EnterSorterStateMachine(**kwargs), moves, metal, classification, prompts
    return make


def test_nothing_is_sensed_until_enter(machine_factory):
    machine, moves, metal, _, prompts = machine_factory(inputs=["q"])
    calls = []
    metal.is_metal_present = lambda: calls.append("read") or False
    machine.run()
    assert calls == [] and machine.cycles_done == 0
    assert moves == ["level"]                              # levelled at start-up, then just waited


def test_plastic_without_metal_goes_to_the_non_battery_side_then_level(machine_factory):
    machine, moves, _, _, prompts = machine_factory(inputs=["", "q"])
    machine.run()
    assert moves == ["level", "non_battery", "level"]
    assert machine.cycles_done == 1 and len(prompts) == 2
    assert machine.load_cell is None and machine.weight_g == 0.0


def test_only_the_metal_sensor_sends_an_item_to_the_battery_side(machine_factory):
    machine, moves, metal, classification, _ = machine_factory(inputs=["", "", "", "q"])
    metal.value = True                                     # plastic WITH metal: possible hidden battery
    machine.run(max_cycles=1)
    assert moves[-2:] == ["battery", "level"]
    metal.value = False
    classification["plastic"] = False                      # not plastic, but no metal: NOT a battery
    machine.run(max_cycles=2)
    assert moves[-2:] == ["non_battery", "level"]
    metal.value = True                                     # not plastic, with metal
    machine.run(max_cycles=3)
    assert moves[-2:] == ["battery", "level"]


def test_cycles_limit_and_closed_stdin_both_stop(machine_factory):
    machine, moves, _, _, _ = machine_factory(inputs=["", "", "", ""])
    machine.run(max_cycles=2)
    assert machine.cycles_done == 2
    machine2, _, _, _, _ = machine_factory(inputs=[])     # input() raises EOFError straight away
    machine2.run()
    assert machine2.cycles_done == 0 and machine2.stop_requested


def test_auto_never_prompts(machine_factory):
    machine, moves, _, _, prompts = machine_factory(inputs=[], auto=True)
    machine.run(max_cycles=3)
    assert prompts == [] and machine.cycles_done == 3


def test_a_failed_classification_returns_to_level_and_keeps_going(machine_factory, monkeypatch):
    machine, moves, _, _, _ = machine_factory(inputs=["", "", "q"])
    boom = {"n": 0}

    def flaky(frame):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("gemini timed out")
        return {"plastic": True}
    monkeypatch.setattr(main_enter, "classify_material", flaky)
    machine.run()
    assert moves == ["level", "level", "non_battery", "level"]    # item 1: error -> straight back to level; item 2: sorted
    assert machine.state is State.WAITING


def test_close_levels_the_bed_and_releases_everything(machine_factory):
    machine, moves, _, _, _ = machine_factory(inputs=["q"])
    machine.close()
    assert moves[-3:] == ["level", "closed", "metal closed"]
