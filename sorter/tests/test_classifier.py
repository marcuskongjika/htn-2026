"""Mock tests for the Gemini material classifier — no network, no API key.

Two layers of mocking are exercised:

1. `classify_material(..., mock=True)` — the built-in mock path that returns a
   canned classification without ever importing the SDK (config.MOCK_HARDWARE).
2. A *fake Gemini SDK* injected into sys.modules, so `_call_gemini` runs its
   real request-construction and response-parsing code against a stub client.
   This is the "mock test setup for Gemini": it verifies we pass the JPEG bytes
   with the correct mime type and the prompt, and that we parse `response.text`
   — all offline.

Run with:
    cd sorter
    python -m pytest tests/test_classifier.py -v
"""
from __future__ import annotations

import sys
import time
import types as pytypes
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from vision import classifier
from vision.classifier import (
    _FALLBACK,
    _MOCK_RESULT,
    _parse_response_text,
    _validate,
    classify_material,
)

JPEG_MAGIC = b"\xff\xd8\xff"
FAKE_JPEG = JPEG_MAGIC + b"\x00fake-jpeg-body\xff\xd9"


# --- built-in mock path ----------------------------------------------------
def test_mock_mode_returns_canned_result_without_network():
    result = classify_material(FAKE_JPEG, mock=True)
    assert result == _MOCK_RESULT
    assert result is not _MOCK_RESULT  # returns a copy, not the shared dict


# --- response parsing ------------------------------------------------------
def test_parse_plain_json():
    r = _parse_response_text('{"plastic_confidence": 0.2, "likely_contains_battery": false, "confidence": 0.8}')
    assert r.plastic is False  # 0.2 <= 0.5 threshold
    assert r.plastic_confidence == 0.2
    assert r.likely_contains_battery is False
    assert r.confidence == 0.8


def test_parse_strips_markdown_fences():
    fenced = '```json\n{"plastic_confidence": 0.9, "likely_contains_battery": true, "confidence": 0.95}\n```'
    r = _parse_response_text(fenced)
    assert r.plastic is True  # 0.9 > 0.5 threshold
    assert r.likely_contains_battery is True


@pytest.mark.parametrize(
    "confidence,expected_plastic",
    [
        (0.6, True),
        (0.51, True),
        (0.5, False),   # strictly greater than the threshold, so 0.5 is NOT plastic
        (0.4, False),
        (0.0, False),
        (1.0, True),
    ],
)
def test_plastic_threshold_at_half(confidence, expected_plastic):
    r = _validate({"plastic_confidence": confidence, "likely_contains_battery": False, "confidence": 0.9})
    assert r.plastic is expected_plastic
    assert r.plastic_confidence == confidence


@pytest.mark.parametrize(
    "bad",
    [
        {"plastic_confidence": 1.5, "likely_contains_battery": True, "confidence": 0.5},
        {"plastic_confidence": "high", "likely_contains_battery": True, "confidence": 0.5},
        {"plastic_confidence": True, "likely_contains_battery": True, "confidence": 0.5},
        {"plastic_confidence": 0.5, "likely_contains_battery": "yes", "confidence": 0.5},
        {"plastic_confidence": 0.5, "likely_contains_battery": True, "confidence": 1.5},
        {"plastic_confidence": 0.5, "likely_contains_battery": True, "confidence": "high"},
    ],
)
def test_validate_rejects_malformed(bad):
    with pytest.raises(ValueError):
        _validate(bad)


# --- fake-SDK "real" path (offline) ---------------------------------------
@pytest.fixture
def fake_gemini(monkeypatch):
    """Inject a stub `google.genai` so `_call_gemini` runs for real, offline."""
    captured: dict = {"response_text": '{"plastic_confidence": 0.9, "likely_contains_battery": true, "confidence": 0.9}'}

    class _FakePart:
        @staticmethod
        def from_bytes(data, mime_type):
            return {"kind": "part", "data": data, "mime_type": mime_type}

    class _FakeGenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    types_mod = pytypes.ModuleType("google.genai.types")
    types_mod.Part = _FakePart
    types_mod.GenerateContentConfig = _FakeGenerateContentConfig

    class _FakeResponse:
        def __init__(self, text):
            self.text = text

    class _FakeModels:
        def generate_content(self, model, contents, config):  # noqa: A002 - mirror SDK signature
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return _FakeResponse(captured["response_text"])

    class _FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.models = _FakeModels()

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.Client = _FakeClient
    genai_mod.types = types_mod

    google_mod = pytypes.ModuleType("google")
    google_mod.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key", raising=False)
    return captured


def test_call_gemini_sends_jpeg_bytes_and_prompt(fake_gemini):
    result = classify_material(FAKE_JPEG, mock=False)
    assert result == {
        "plastic": True,  # plastic_confidence 0.9 > 0.5
        "plastic_confidence": 0.9,
        "likely_contains_battery": True,
        "confidence": 0.9,
    }

    # The image Part must carry our exact JPEG bytes with the image/jpeg mime type.
    part, prompt = fake_gemini["contents"]
    assert part == {"kind": "part", "data": FAKE_JPEG, "mime_type": "image/jpeg"}
    assert "STRICT JSON" in prompt
    assert fake_gemini["api_key"] == "test-key"
    assert fake_gemini["model"] == config.GEMINI_MODEL


def test_malformed_model_output_falls_back_to_caution(fake_gemini):
    fake_gemini["response_text"] = "sorry, I can't do that"
    result = classify_material(FAKE_JPEG, mock=False)
    assert result == _FALLBACK
    assert result["likely_contains_battery"] is False   # an unreachable model claims nothing


# --- failure handling ------------------------------------------------------
def test_exception_falls_back_to_caution(monkeypatch):
    def boom(_jpeg):
        raise RuntimeError("network down")

    monkeypatch.setattr(classifier, "_call_gemini", boom)
    result = classify_material(FAKE_JPEG, mock=False)
    assert result == _FALLBACK
    assert result["likely_contains_battery"] is False   # an unreachable model claims nothing


def test_timeout_falls_back_to_caution(monkeypatch):
    def slow(_jpeg):
        time.sleep(1.0)
        raise AssertionError("should have timed out before returning")

    monkeypatch.setattr(classifier, "_call_gemini", slow)
    monkeypatch.setattr(config, "GEMINI_TIMEOUT_S", 0.1, raising=False)
    result = classify_material(FAKE_JPEG, mock=False)
    assert result == _FALLBACK
    assert result["likely_contains_battery"] is False   # an unreachable model claims nothing
