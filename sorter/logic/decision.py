"""Decision fusion: combine sensor and vision signals into a bin verdict.

Pure function, no I/O — easily unit-testable in isolation from any hardware
or network call (see tests/test_decision.py).
"""
from __future__ import annotations

Verdict = str  # "safe" | "flagged"

SAFE: Verdict = "safe"
FLAGGED: Verdict = "flagged"


def fuse(weight_g: float, metal_present: bool, material_result: dict) -> Verdict:
    """Fuse sensor readings and Gemini's material classification into a verdict.

    Fusion rule (deliberately simple and conservative for a 32-hour build):
        flagged if metal_present OR material_result["likely_contains_battery"]
        else safe

    Rationale:
    - The inductive sensor is a fast, cheap, high-precision signal for bare
      metal (battery terminals, casings) — any positive hit is enough to
      flag on its own, no need to corroborate it with vision.
    - Gemini's `likely_contains_battery` already encodes visual cues the
      metal sensor can miss (a battery sealed inside plastic/cardboard with
      no exposed metal). It fails toward True on classifier errors/timeouts
      (see vision/classifier.py), so treating it as a standalone flag signal
      is safe by construction.
    - `weight_g` is intentionally NOT used in the fusion rule yet. It's
      captured and passed through as a trigger for entering MEASURING (see
      main.py's WEIGHT_TRIGGER_G) and as telemetry for future tuning (e.g.
      an implausibly heavy small item could become its own signal), but
      there isn't enough hackathon time to derive a reliable weight-based
      battery heuristic, so it's reserved rather than guessed at.
    """
    likely_battery = bool(material_result.get("likely_contains_battery", True))

    if metal_present or likely_battery:
        return FLAGGED
    return SAFE
