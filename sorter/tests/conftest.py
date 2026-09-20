"""Shared pytest setup."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


@pytest.fixture(autouse=True)
def isolated_calibration(tmp_path, monkeypatch):
    """Tests never see (or overwrite) the rig's real servo_calibration.json."""
    path = tmp_path / "servo_calibration.json"
    monkeypatch.setattr(config, "SERVO_CALIBRATION_FILE", path)
    return path
