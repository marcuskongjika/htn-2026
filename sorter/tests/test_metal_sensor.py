"""sensors.metal_sensor against gpiozero's mock pins - no hardware.

Run with:
    cd sorter
    python -m pytest tests/test_metal_sensor.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

gpiozero = pytest.importorskip("gpiozero")
from gpiozero.pins.mock import MockFactory

import config
from sensors.metal_sensor import MetalSensor


@pytest.fixture
def pins(monkeypatch):
    factory = MockFactory()
    monkeypatch.setattr(gpiozero.Device, "pin_factory", factory)
    yield factory
    factory.reset()


def test_uses_gpio17_with_the_internal_pull_up(pins):
    assert config.METAL_SENSOR_PIN == 17 and config.METAL_SENSOR_PULL_UP is True
    sensor = MetalSensor(mock=False)
    assert pins.pin(17).pull == "up"
    sensor.close()


def test_line_low_means_metal(pins):
    sensor = MetalSensor(mock=False)
    line = pins.pin(17)
    line.drive_high()                                  # idle: pulled up, nothing there
    assert sensor.is_metal_present(samples=3, interval_s=0) is False
    line.drive_low()                                   # NPN output pulls the line to GND
    assert sensor.is_metal_present(samples=3, interval_s=0) is True
    sensor.close()


def test_external_pull_up_option_is_still_active_low(pins, monkeypatch):
    monkeypatch.setattr(config, "METAL_SENSOR_PULL_UP", False)
    sensor = MetalSensor(mock=False)
    line = pins.pin(17)
    assert line.pull == "floating"
    line.drive_low()
    assert sensor.is_metal_present(samples=3, interval_s=0) is True
    line.drive_high()
    assert sensor.is_metal_present(samples=3, interval_s=0) is False
    sensor.close()


def test_mock_mode_never_touches_gpio():
    sensor = MetalSensor(mock=True)
    assert sensor.is_metal_present(samples=3, interval_s=0) in (True, False)
    sensor.close()
