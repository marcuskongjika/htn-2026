"""Decision rule: the induction sensor decides which side the item goes to.

Pure functions, no I/O — easily unit-testable in isolation from any hardware
or network call (see tests/test_decision.py).
"""
from __future__ import annotations

from config import BATTERY_SIDE, NON_BATTERY_SIDE

__all__ = ["BATTERY_SIDE", "NON_BATTERY_SIDE", "sort_side", "describe"]


def sort_side(plastic: bool, metal_present: bool) -> str:
    """Which side an item goes to. The induction sensor is the ONLY thing that decides:

        metal_present      -> BATTERY_SIDE       no two ways about it
        not metal_present  -> NON_BATTERY_SIDE

    `plastic` (the camera / Gemini) does not move the item either way. It can't see inside
    anything, so it is never allowed to put an item on the battery side by itself, and it can
    never talk an item with metal in it OUT of the battery side. Nothing here assumes a
    battery: not Gemini's `likely_contains_battery` guess, and not "it isn't plastic, so
    maybe". A failed or timed-out classification therefore changes nothing about where the
    item goes - the sensor reading still stands.

    What vision is for is the explanation (see describe()): plastic + metal is the case this
    machine exists to catch - metal hidden inside something that looks like plain plastic.

    These are names, not directions. Which physical end of the bed each one is lives in
    config (BATTERY_SIDE_SETPOINT); ServoPair.go_to() accepts the names directly, so
    main.py stays a one-liner: `pair.go_to(sort_side(...))`.
    """
    return BATTERY_SIDE if metal_present else NON_BATTERY_SIDE


def describe(plastic: bool, metal_present: bool) -> str:
    """One line for the log / a screen: what the two signals say together. Never affects sorting."""
    if metal_present and plastic:
        return "looks like plastic but metal detected inside - possible hidden battery"
    if metal_present:
        return "metal detected"
    if plastic:
        return "plastic, no metal detected"
    return "not plastic, no metal detected"
