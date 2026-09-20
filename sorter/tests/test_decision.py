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

from logic.decision import OTHER_BIN, PLASTIC_BIN, decide_side, sort_side


def test_plastic_and_no_metal_goes_to_plastic_bin():
    assert sort_side(plastic=True, metal_present=False) == PLASTIC_BIN


def test_plastic_but_metal_goes_to_other_bin():
    # Metal is a veto: even something vision calls plastic goes the other way.
    assert sort_side(plastic=True, metal_present=True) == OTHER_BIN


def test_not_plastic_no_metal_goes_to_other_bin():
    assert sort_side(plastic=False, metal_present=False) == OTHER_BIN


def test_not_plastic_and_metal_goes_to_other_bin():
    assert sort_side(plastic=False, metal_present=True) == OTHER_BIN


def test_plastic_bin_only_for_clean_plastic():
    # Exactly one of the four combinations reaches the plastic bin.
    combos = [(p, m) for p in (True, False) for m in (True, False)]
    to_plastic = [(p, m) for (p, m) in combos if sort_side(p, m) == PLASTIC_BIN]
    assert to_plastic == [(True, False)]


def test_decide_side_none_when_nothing_detected():
    # No metal and not plastic -> nothing to sort, stay level.
    assert decide_side(plastic=False, metal_present=False) is None


def test_decide_side_plastic_no_metal_goes_to_plastic_bin():
    assert decide_side(plastic=True, metal_present=False) == PLASTIC_BIN


def test_decide_side_metal_goes_to_other_bin():
    assert decide_side(plastic=True, metal_present=True) == OTHER_BIN
    assert decide_side(plastic=False, metal_present=True) == OTHER_BIN


_TESTS = [
    test_plastic_and_no_metal_goes_to_plastic_bin,
    test_plastic_but_metal_goes_to_other_bin,
    test_not_plastic_no_metal_goes_to_other_bin,
    test_not_plastic_and_metal_goes_to_other_bin,
    test_plastic_bin_only_for_clean_plastic,
    test_decide_side_none_when_nothing_detected,
    test_decide_side_plastic_no_metal_goes_to_plastic_bin,
    test_decide_side_metal_goes_to_other_bin,
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
