"""Decision rule: turn the sensor + vision signals into a tilt direction.

Pure function, no I/O — easily unit-testable in isolation from any hardware
or network call (see tests/test_decision.py).
"""
from __future__ import annotations

# ServoPair setpoint names (actuators/servo_pair.py: go_min / go_max). Returning
# these directly keeps main.py a one-liner: `pair.go_to(sort_side(...))`.
PLASTIC_BIN: str = "min"   # plastic AND no metal  -> go_min
OTHER_BIN: str = "max"     # every other case      -> go_max


def sort_side(plastic: bool, metal_present: bool) -> str:
    """Which tilt setpoint an item goes to.

    Rule (deliberately simple):
        plastic AND NOT metal_present -> PLASTIC_BIN ("min")
        every other case             -> OTHER_BIN  ("max")

    Rationale:
    - The plastic bin is only for clean plastic: Gemini must say plastic AND the
      inductive sensor must read no metal. A metal hit is a fast, high-precision
      veto (metal-cased items, foil-lined packaging), so any positive metal
      reading sends the item to the other side regardless of what vision saw.
    - Gemini's `likely_contains_battery` is still reported by the classifier for
      logging, but it does NOT affect direction — sorting is purely plastic-vs-not
      with metal as an override.
    - The classifier fails toward `plastic=False` on errors/timeouts (see
      vision/classifier.py), so an unreachable model sends the item to OTHER_BIN,
      the conservative default here.

    Flipping which physical side is which is a one-line change: swap the two
    constants above.
    """
    if plastic and not metal_present:
        return PLASTIC_BIN
    return OTHER_BIN


def decide_side(plastic: bool, metal_present: bool) -> str | None:
    """Tilt setpoint for a *detected* item, or None when nothing sortable is present.

    An item counts as detected if the metal sensor fired OR Gemini called it
    plastic. With no metal and no plastic there is nothing on the bed worth
    sorting, so the caller should stay at level and keep sensing (return None)
    rather than tilt an empty bed. When something is detected, the direction is
    the usual `sort_side()` rule.
    """
    if not plastic and not metal_present:
        return None
    return sort_side(plastic, metal_present)
