"""Unit tests for logic.decision.fuse — pure function, no I/O, no mocking needed.

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

from logic.decision import FLAGGED, SAFE, fuse

NOT_BATTERY = {"material": "banana peel", "likely_contains_battery": False, "confidence": 0.9}
IS_BATTERY = {"material": "plastic toy", "likely_contains_battery": True, "confidence": 0.9}


def test_safe_when_no_metal_and_no_battery_material():
    assert fuse(weight_g=50.0, metal_present=False, material_result=NOT_BATTERY) == SAFE


def test_flagged_when_metal_present_even_if_material_looks_safe():
    assert fuse(weight_g=50.0, metal_present=True, material_result=NOT_BATTERY) == FLAGGED


def test_flagged_when_material_says_battery_even_without_metal():
    assert fuse(weight_g=50.0, metal_present=False, material_result=IS_BATTERY) == FLAGGED


def test_flagged_when_both_metal_and_battery_material():
    assert fuse(weight_g=50.0, metal_present=True, material_result=IS_BATTERY) == FLAGGED


def test_weight_does_not_affect_verdict():
    # Weight is reserved for future tuning; it must not change the outcome.
    low = fuse(weight_g=0.1, metal_present=False, material_result=NOT_BATTERY)
    high = fuse(weight_g=5000.0, metal_present=False, material_result=NOT_BATTERY)
    assert low == high == SAFE


def test_missing_likely_contains_battery_key_fails_toward_caution():
    # A malformed/partial material_result must not accidentally read as safe.
    assert fuse(weight_g=50.0, metal_present=False, material_result={"material": "?"}) == FLAGGED


_TESTS = [
    test_safe_when_no_metal_and_no_battery_material,
    test_flagged_when_metal_present_even_if_material_looks_safe,
    test_flagged_when_material_says_battery_even_without_metal,
    test_flagged_when_both_metal_and_battery_material,
    test_weight_does_not_affect_verdict,
    test_missing_likely_contains_battery_key_fails_toward_caution,
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
