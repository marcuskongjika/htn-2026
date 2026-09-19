"""HX711 + load cell wrapper.

Real mode bit-bangs the HX711 protocol on the configured GPIO pins using
gpiozero. Mock mode (MOCK_HARDWARE=1) returns a randomized weight around a
configurable baseline so the rest of the pipeline can be exercised with no
hardware attached.

Standalone bring-up:
    cd sorter
    python -m sensors.load_cell
"""
from __future__ import annotations

import logging
import random
import time

import config

log = logging.getLogger(__name__)

# Number of HX711 ADC bits.
_HX711_BITS = 24
_HX711_SIGN_BIT = 1 << 23
_HX711_FULL_SCALE = 1 << 24


class LoadCell:
    """Wraps an HX711 amplifier feeding a single load cell.

    In real mode this bit-bangs the HX711 serial protocol directly on
    DOUT/SCK using gpiozero pins, since HX711 is a simple synchronous
    protocol (no library dependency needed beyond GPIO access).
    """

    def __init__(self, mock: bool | None = None) -> None:
        self.mock = config.MOCK_HARDWARE if mock is None else mock
        self._zero_offset = config.HX711_ZERO_OFFSET
        self._reference_unit = config.HX711_REFERENCE_UNIT
        self._mock_current_g = config.MOCK_WEIGHT_BASELINE_G

        if self.mock:
            log.info("LoadCell running in MOCK mode (no GPIO access)")
            self._dout = None
            self._sck = None
        else:
            from gpiozero import DigitalInputDevice, DigitalOutputDevice

            self._dout = DigitalInputDevice(config.HX711_DOUT_PIN, pull_up=False)
            self._sck = DigitalOutputDevice(config.HX711_SCK_PIN)
            self._sck.off()
            log.info(
                "LoadCell initialized on DOUT=GPIO%d SCK=GPIO%d",
                config.HX711_DOUT_PIN,
                config.HX711_SCK_PIN,
            )

    # -- low-level HX711 protocol -------------------------------------------------
    def _read_raw(self) -> int:
        """Read one 24-bit two's-complement sample from the HX711."""
        if self.mock:
            jitter = random.uniform(-config.MOCK_WEIGHT_JITTER_G, config.MOCK_WEIGHT_JITTER_G)
            grams = self._mock_current_g + jitter
            return int(grams * self._reference_unit) + self._zero_offset

        # Wait for DOUT to go low, signalling data ready.
        timeout = time.monotonic() + 1.0
        while self._dout.value == 1:
            if time.monotonic() > timeout:
                raise TimeoutError("HX711 not ready (DOUT stayed high)")
            time.sleep(0.001)

        count = 0
        for _ in range(_HX711_BITS):
            self._sck.on()
            count = (count << 1) | self._dout.value
            self._sck.off()

        # 25th pulse selects gain/channel for the *next* read (channel A, gain 128).
        self._sck.on()
        self._sck.off()

        if count & _HX711_SIGN_BIT:
            count -= _HX711_FULL_SCALE
        return count

    # -- public API -------------------------------------------------------------
    def tare(self, samples: int = 15) -> None:
        """Zero the scale by averaging `samples` raw readings as the new offset."""
        readings = [self._read_raw() for _ in range(samples)]
        self._zero_offset = sum(readings) // len(readings)
        log.info("Tared load cell: zero_offset=%d", self._zero_offset)

    def read_weight_g(self) -> float:
        """Return a single weight sample in grams (not debounced/averaged)."""
        raw = self._read_raw()
        grams = (raw - self._zero_offset) / self._reference_unit
        return grams

    def stable_reading(
        self,
        samples: int = config.WEIGHT_STABLE_SAMPLES,
        tolerance: float = config.WEIGHT_STABLE_TOLERANCE_G,
        max_attempts: int = config.WEIGHT_STABLE_MAX_ATTEMPTS,
    ) -> float:
        """Average readings until they settle within `tolerance` grams of
        each other, or give up after `max_attempts` batches and return the
        last average anyway.
        """
        last_avg = 0.0
        for attempt in range(1, max_attempts + 1):
            batch = [self.read_weight_g() for _ in range(samples)]
            spread = max(batch) - min(batch)
            last_avg = sum(batch) / len(batch)
            log.debug(
                "stable_reading attempt=%d avg=%.2fg spread=%.2fg", attempt, last_avg, spread
            )
            if spread <= tolerance:
                return last_avg
            time.sleep(0.02)

        log.warning(
            "stable_reading did not settle within %d attempts (tolerance=%.2fg); "
            "returning last average %.2fg",
            max_attempts,
            tolerance,
            last_avg,
        )
        return last_avg


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cell = LoadCell()
    print(f"MOCK_HARDWARE={config.MOCK_HARDWARE}")
    print("Taring...")
    cell.tare()
    print("Streaming live readings (Ctrl+C to stop):")
    try:
        while True:
            print(f"  weight = {cell.read_weight_g():7.2f} g")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nStopped.")
