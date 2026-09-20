"""Gemini-based material classifier.

Wraps the current `google-genai` SDK (NOT the deprecated
`google-generativeai`) to classify a captured JPEG frame: whether the item
is plastic and whether it likely contains a hidden battery.

The model reports a numeric `plastic_confidence`; this module derives the
`plastic` boolean by thresholding it at `config.PLASTIC_CONFIDENCE_THRESHOLD`
(0.5), so the cutoff is enforced in code rather than trusted to the model.

The model is instructed to return strict JSON only. The response is parsed
and validated defensively — a malformed/non-JSON response, a network error,
or a timeout all fall back to a cautious default rather than crashing the
pipeline or silently calling something "safe".

Mock mode returns a canned classification without calling the network.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
from dataclasses import dataclass, asdict

import config

log = logging.getLogger(__name__)

_PROMPT = """You are inspecting a single item on a waste-sorting conveyor for the \
presence of a hidden battery (e.g. inside a toy, remote, greeting card, vape, \
or electronic device).

Look at the photo and respond with STRICT JSON only, no markdown fences, no \
prose, matching exactly this schema:
{"plastic_confidence": number, "likely_contains_battery": bool, "confidence": number}

- "plastic_confidence": your confidence, from 0.0 to 1.0, that the item's \
primary material is plastic (e.g. a plastic bottle, container, toy, or \
wrapper). Use values below 0.5 for clearly non-plastic items (metal, paper, \
cardboard, glass, food/organics).
- "likely_contains_battery": true if the item plausibly contains a battery, \
even if not visible (electronics, toys with buttons/lights, remotes, small \
sealed devices). When uncertain, prefer true.
- "confidence": your confidence in the battery judgment, from 0.0 to 1.0.

Respond with the JSON object only."""

# Fail toward caution, not toward "safe", whenever the model can't be reached
# or its response can't be trusted. The battery signal stays True (flag it);
# plastic doesn't affect sorting, so its cautious default is simply False.
_FALLBACK = {"plastic": False, "plastic_confidence": 0.0, "likely_contains_battery": True, "confidence": 0.0}

_MOCK_RESULT = {"plastic": True, "plastic_confidence": 0.87, "likely_contains_battery": True, "confidence": 0.87}


@dataclass
class ClassificationResult:
    plastic: bool
    plastic_confidence: float
    likely_contains_battery: bool
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


def _validate(raw: dict) -> ClassificationResult:
    plastic_confidence = raw.get("plastic_confidence")
    likely = raw.get("likely_contains_battery")
    confidence = raw.get("confidence")

    if not isinstance(plastic_confidence, (int, float)) or isinstance(plastic_confidence, bool) \
            or not (0.0 <= float(plastic_confidence) <= 1.0):
        raise ValueError(f"invalid 'plastic_confidence': {plastic_confidence!r}")
    if not isinstance(likely, bool):
        raise ValueError(f"invalid 'likely_contains_battery': {likely!r}")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
            or not (0.0 <= float(confidence) <= 1.0):
        raise ValueError(f"invalid 'confidence': {confidence!r}")

    plastic_confidence = float(plastic_confidence)
    return ClassificationResult(
        plastic=plastic_confidence > config.PLASTIC_CONFIDENCE_THRESHOLD,
        plastic_confidence=plastic_confidence,
        likely_contains_battery=likely,
        confidence=float(confidence),
    )


def _parse_response_text(text: str) -> ClassificationResult:
    text = text.strip()
    # Strip markdown code fences if the model added them despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json"):]
        text = text.strip()

    raw = json.loads(text)  # raises json.JSONDecodeError on malformed output
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    return _validate(raw)


def _call_gemini(jpeg_bytes: bytes) -> ClassificationResult:
    from google import genai
    from google.genai import types

    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
            _PROMPT,
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return _parse_response_text(response.text)


def classify_material(jpeg_bytes: bytes, mock: bool | None = None) -> dict:
    """Classify whether the item in `jpeg_bytes` is plastic and assess battery risk.

    Returns a dict:
        {"plastic": bool, "plastic_confidence": float,
         "likely_contains_battery": bool, "confidence": float}
    where `plastic` is `plastic_confidence > config.PLASTIC_CONFIDENCE_THRESHOLD`.
    Never raises — on any error or timeout, returns the cautious fallback.
    """
    use_mock = config.MOCK_HARDWARE if mock is None else mock

    if use_mock:
        log.info("Classifier MOCK mode: returning canned classification")
        return dict(_MOCK_RESULT)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_call_gemini, jpeg_bytes)
        try:
            result = future.result(timeout=config.GEMINI_TIMEOUT_S)
            log.info("Gemini classification: %s", result.to_dict())
            return result.to_dict()
        except concurrent.futures.TimeoutError:
            log.error("Gemini call timed out after %.1fs; falling back to cautious default", config.GEMINI_TIMEOUT_S)
            return dict(_FALLBACK)
        except Exception:
            log.exception("Gemini call failed; falling back to cautious default")
            return dict(_FALLBACK)


if __name__ == "__main__":
    import sys

    from vision.camera import capture_frame

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"MOCK_HARDWARE={config.MOCK_HARDWARE}")
    frame = capture_frame()
    result = classify_material(frame)
    json.dump(result, sys.stdout, indent=2)
    print()
