"""Load cell on an HX711: raw counts -> grams.

Real mode reads the HX711 through sensors/hx711.py (DOUT/SCK pins from config).
Mock mode (MOCK_HARDWARE=1) returns a randomized weight around a configurable
baseline so the rest of the pipeline can be exercised with no hardware attached.

    grams = (raw - zero_offset) / reference_unit        reference_unit = counts per gram

Both numbers come from the saved calibration (config.LOAD_CELL_CALIBRATION_FILE,
written by `python tests/load_cell_read.py`), falling back to the placeholders in
config. The zero drifts with temperature and with whatever is bolted to the cell,
so tare() at start-up and after every dump; the scale (counts per gram) is a
property of the cell and only needs measuring once.

Standalone bring-up:
    cd sorter
    python -m sensors.load_cell            # tare, then stream grams
    python tests/load_cell_read.py         # interactive: tare / calibrate / save
"""
from __future__ import annotations

import json
import logging
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger(__name__)


def load_calibration(path: Path | None = None) -> dict:
    """{"zero_offset": int, "reference_unit": float} from the saved file, else config's placeholders."""
    path = Path(path or config.LOAD_CELL_CALIBRATION_FILE)
    if path.exists():
        data = json.loads(path.read_text())
        return {"zero_offset": int(data["zero_offset"]), "reference_unit": float(data["reference_unit"])}
    return {"zero_offset": config.HX711_ZERO_OFFSET, "reference_unit": config.HX711_REFERENCE_UNIT}


def save_calibration(zero_offset: int, reference_unit: float, path: Path | None = None, note: str = "") -> Path:
    path = Path(path or config.LOAD_CELL_CALIBRATION_FILE)
    path.write_text(json.dumps({
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "note": note or "grams = (raw - zero_offset) / reference_unit. Written by tests/load_cell_read.py",
        "zero_offset": int(zero_offset),
        "reference_unit": float(reference_unit),
    }, indent=2) + "\n")
    return path


class LoadCell:
    """An HX711 amplifier feeding a single load cell."""

    def __init__(self, mock: bool | None = None, hx711=None) -> None:
        """hx711: an already-built driver (the tests pass one on fake pins)."""
        self.mock = config.MOCK_HARDWARE if mock is None else mock
        calibration = load_calibration()
        self._zero_offset = calibration["zero_offset"]
        self._reference_unit = calibration["reference_unit"]
        self._mock_current_g = config.MOCK_WEIGHT_BASELINE_G
        self._hx = None

        if self.mock:
            log.info("LoadCell running in MOCK mode (no GPIO access)")
            return

        if hx711 is None:
            from sensors.hx711 import HX711

            hx711 = HX711(config.HX711_DOUT_PIN, config.HX711_SCK_PIN, gain=config.HX711_GAIN)
        self._hx = hx711
        log.info("LoadCell on DOUT=GPIO%d SCK=GPIO%d, zero=%d, %.3f counts/g",
                 config.HX711_DOUT_PIN, config.HX711_SCK_PIN, self._zero_offset, self._reference_unit)

    # -- calibration ------------------------------------------------------------------
    @property
    def zero_offset(self) -> int:
        return self._zero_offset

    @property
    def reference_unit(self) -> float:
        return self._reference_unit

    def read_raw(self, samples: int = 1) -> int:
        """Raw HX711 counts (median of `samples`)."""
        if self.mock:
            jitter = random.uniform(-config.MOCK_WEIGHT_JITTER_G, config.MOCK_WEIGHT_JITTER_G)
            return int((self._mock_current_g + jitter) * self._reference_unit) + self._zero_offset
        return self._hx.read_raw() if samples <= 1 else self._hx.read_median(samples)

    def _read_raw(self) -> int:  # kept for callers of the old private name
        return self.read_raw()

    def tare(self, samples: int = 15) -> int:
        """Zero the scale: the median of `samples` raw readings becomes the new offset."""
        self._zero_offset = int(statistics.median(self.read_raw() for _ in range(samples)))
        log.info("Tared load cell: zero_offset=%d", self._zero_offset)
        return self._zero_offset

    def calibrate(self, known_grams: float, samples: int = 15) -> float:
        """With a known weight on the (already tared) scale, work out counts per gram."""
        if known_grams <= 0:
            raise ValueError("known weight must be positive")
        loaded = statistics.median(self.read_raw() for _ in range(samples))
        unit = (loaded - self._zero_offset) / known_grams
        if abs(unit) < 1e-6:
            raise ValueError("the reading did not change with the weight on - is it on the cell, and was the scale tared empty?")
        self._reference_unit = unit
        log.info("Calibrated load cell: %.3f counts/g%s", unit, "  (negative: A+/A- are swapped, handled in software)" if unit < 0 else "")
        return unit

    def save(self) -> Path:
        return save_calibration(self._zero_offset, self._reference_unit)

    # -- public API -------------------------------------------------------------
    def read_weight_g(self, samples: int = 1) -> float:
        """Weight in grams. samples > 1 takes the median of that many reads (10 reads = ~1 s)."""
        return (self.read_raw(samples) - self._zero_offset) / self._reference_unit

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

    def close(self) -> None:
        if self._hx is not None:
            self._hx.close()
            self._hx = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cell = LoadCell()
    print(f"MOCK_HARDWARE={config.MOCK_HARDWARE}")
    print("Taring (keep the scale empty)...")
    cell.tare()
    print("Streaming live readings (Ctrl+C to stop):")
    try:
        while True:
            print(f"  weight = {cell.read_weight_g(samples=3):8.2f} g")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cell.close()
