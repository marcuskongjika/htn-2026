"""Inductive proximity sensor (LJ18A3-8-Z/BX) wrapper.

Real mode reads the GPIO digital input, active-low (LOW = metal detected),
using the Pi's internal pull-up (the NPN output only pulls to GND), with a
simple majority-vote debounce. Mock mode returns a randomly toggling
simulated trigger so the rest of the pipeline can be exercised with no
hardware attached.

Standalone bring-up:
    cd sorter
    python -m sensors.metal_sensor
"""
from __future__ import annotations

import logging
import random
import time

import config

log = logging.getLogger(__name__)


class MetalSensor:
    def __init__(self, mock: bool | None = None) -> None:
        self.mock = config.MOCK_HARDWARE if mock is None else mock

        if self.mock:
            log.info("MetalSensor running in MOCK mode (no GPIO access)")
            self._pin = None
        else:
            from gpiozero import DigitalInputDevice

            if config.METAL_SENSOR_PULL_UP:
                # Internal pull-up holds the line high; gpiozero then treats LOW as "active".
                self._pin = DigitalInputDevice(config.METAL_SENSOR_PIN, pull_up=True)
            else:
                # An external pull-up is fitted: leave the pin floating, still active-low.
                self._pin = DigitalInputDevice(config.METAL_SENSOR_PIN, pull_up=None, active_state=False)
            log.info("MetalSensor initialized on GPIO%d (active-low, %s pull-up)", config.METAL_SENSOR_PIN,
                     "internal" if config.METAL_SENSOR_PULL_UP else "external")

    def _raw_metal_present(self) -> bool:
        if self.mock:
            return random.random() < config.MOCK_METAL_TRIGGER_PROBABILITY
        # Active-low is handled by gpiozero (see __init__): active = line LOW = metal detected.
        return bool(self._pin.is_active)

    def close(self) -> None:
        if self._pin is not None:
            self._pin.close()
            self._pin = None

    def is_metal_present(
        self,
        samples: int = config.METAL_SENSOR_DEBOUNCE_SAMPLES,
        interval_s: float = config.METAL_SENSOR_DEBOUNCE_S,
    ) -> bool:
        """Debounced read: majority vote across `samples` consecutive polls."""
        votes = []
        for _ in range(samples):
            votes.append(self._raw_metal_present())
            time.sleep(interval_s)
        present = sum(votes) > len(votes) / 2
        log.debug("metal debounce votes=%s -> present=%s", votes, present)
        return present


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sensor = MetalSensor()
    print(f"MOCK_HARDWARE={config.MOCK_HARDWARE}")
    print("Polling for metal (Ctrl+C to stop):")
    try:
        while True:
            present = sensor.is_metal_present()
            print(f"  metal_present = {present}")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nStopped.")
