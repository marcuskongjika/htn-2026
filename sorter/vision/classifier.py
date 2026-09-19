"""Gemini-based material classifier.

Wraps the current `google-genai` SDK (NOT the deprecated
`google-generativeai`) to classify a captured JPEG frame's material and
whether it likely contains a hidden battery.

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
{"material": string, "likely_contains_battery": bool, "confidence": number}

- "material": a short description of the primary material/object (e.g. \
"plastic toy", "cardboard", "banana peel", "electronic remote").
- "likely_contains_battery": true if the item plausibly contains a battery, \
even if not visible (electronics, toys with buttons/lights, remotes, small \
sealed devices). When uncertain, prefer true.
- "confidence": your confidence in this judgment, from 0.0 to 1.0.

Respond with the JSON object only."""

# Fail toward caution, not toward "safe", whenever the model can't be reached
# or its response can't be trusted.
_FALLBACK = {"material": "unknown", "likely_contains_battery": True, "confidence": 0.0}

_MOCK_RESULT = {"material": "plastic toy", "likely_contains_battery": True, "confidence": 0.87}


@dataclass
class ClassificationResult:
    material: str
    likely_contains_battery: bool
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


def _validate(raw: dict) -> ClassificationResult:
    material = raw.get("material")
    likely = raw.get("likely_contains_battery")
    confidence = raw.get("confidence")

    if not isinstance(material, str) or not material.strip():
        raise ValueError(f"invalid 'material': {material!r}")
    if not isinstance(likely, bool):
        raise ValueError(f"invalid 'likely_contains_battery': {likely!r}")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        raise ValueError(f"invalid 'confidence': {confidence!r}")

    return ClassificationResult(material=material, likely_contains_battery=likely, confidence=float(confidence))


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
    """Classify the material in `jpeg_bytes` and assess battery risk.

    Returns a dict: {"material": str, "likely_contains_battery": bool, "confidence": float}.
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
