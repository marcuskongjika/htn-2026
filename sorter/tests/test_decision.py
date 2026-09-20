"""Unit tests for logic.decision.sort_side — pure function, no I/O, no mocking.

Run with:
    cd sorter
    python -m pytest tests/test_decision.py -v
or, with no test runner installed:
    python -m tests.test_decision
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic.decision import BATTERY_SIDE, NON_BATTERY_SIDE, describe, sort_side


def test_metal_always_means_the_battery_side():
    # The induction sensor is the authority: what the camera thinks does not matter.
    assert sort_side(plastic=True, metal_present=True) == BATTERY_SIDE
    assert sort_side(plastic=False, metal_present=True) == BATTERY_SIDE


def test_no_metal_always_means_the_non_battery_side():
    assert sort_side(plastic=True, metal_present=False) == NON_BATTERY_SIDE
    # Not plastic is NOT evidence of a battery: paper, glass, food all go here too.
    assert sort_side(plastic=False, metal_present=False) == NON_BATTERY_SIDE


def test_vision_never_changes_the_side():
    for metal in (True, False):
        assert sort_side(plastic=True, metal_present=metal) == sort_side(plastic=False, metal_present=metal)


def test_a_failed_classification_does_not_send_anything_to_the_battery_side():
    from vision.classifier import _FALLBACK, _MOCK_RESULT
    assert _FALLBACK["likely_contains_battery"] is False      # nothing is assumed when the model is unreachable
    assert _MOCK_RESULT["likely_contains_battery"] is False
    plastic = bool(_FALLBACK.get("plastic", False))
    assert sort_side(plastic, metal_present=False) == NON_BATTERY_SIDE
    assert sort_side(plastic, metal_present=True) == BATTERY_SIDE


def test_describe_explains_without_deciding():
    assert "possible hidden battery" in describe(plastic=True, metal_present=True)
    assert describe(plastic=False, metal_present=True) == "metal detected"
    assert describe(plastic=True, metal_present=False) == "plastic, no metal detected"
    assert describe(plastic=False, metal_present=False) == "not plastic, no metal detected"


_TESTS = [
    test_metal_always_means_the_battery_side,
    test_no_metal_always_means_the_non_battery_side,
    test_vision_never_changes_the_side,
    test_a_failed_classification_does_not_send_anything_to_the_battery_side,
    test_describe_explains_without_deciding,
]


if __name__ == "__main__":
    failures = 0
    for test in _TESTS:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    sys.exit(1 if failures else 0)


# --- side names -> physical ends ---------------------------------------------------------
def test_sides_are_named_for_what_goes_in_them():
    assert (BATTERY_SIDE, NON_BATTERY_SIDE) == ("battery", "non_battery")


def test_config_maps_each_side_to_one_end_of_the_bed():
    import config
    assert set(config.SIDE_SETPOINTS.values()) == {"min", "max"}
    assert config.SIDE_SETPOINTS[BATTERY_SIDE] == config.BATTERY_SIDE_SETPOINT


def test_side_names_work_as_setpoints_with_the_rigs_numbers(monkeypatch):
    import config
    from actuators.calibration import Calibration
    cal = Calibration.from_measurements({43: 2144, 13: 3305}, {43: 2430, 13: 3016}, {43: 1842, 13: 3607})
    assert cal[43].setpoint(BATTERY_SIDE) == cal[43].setpoint("max") == 1892
    assert cal[13].setpoint(NON_BATTERY_SIDE) == cal[13].setpoint("min") == 3066
    monkeypatch.setattr(config, "SIDE_SETPOINTS", {BATTERY_SIDE: "min", NON_BATTERY_SIDE: "max"})   # bins swapped over
    assert cal[43].setpoint(BATTERY_SIDE) == cal[43].setpoint("min") == 2380
