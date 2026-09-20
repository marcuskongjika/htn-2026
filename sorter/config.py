"""Central configuration: pin numbers, calibration constants, thresholds,
servo IDs, and environment-loaded secrets/flags.

Nothing in this module touches hardware. It is safe to import from
anywhere, including standalone module bring-up scripts.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the sorter/ package root regardless of current working dir.
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH if _ENV_PATH.exists() else None)


def _env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# --- Mock mode -----------------------------------------------------------
# When True, every hardware-touching module (GPIO, serial, camera) returns
# synthetic data instead of opening a real device. This is the default so
# the pipeline can be demoed on a laptop with zero hardware attached.
MOCK_HARDWARE: bool = _env_flag("MOCK_HARDWARE", default=True)

# --- Gemini API ------------------------------------------------------------
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_TIMEOUT_S: float = 30.0  # gemini-3.6-flash "thinks", so it needs headroom past 5s
# The item is labelled plastic if the model's plastic_confidence exceeds this.
PLASTIC_CONFIDENCE_THRESHOLD: float = 0.5

# --- HX711 / load cell wiring ----------------------------------------------
# BCM numbering. VCC -> Pi 3.3V (pin 1), GND -> pin 6.
HX711_DOUT_PIN: int = 5   # physical pin 29
HX711_SCK_PIN: int = 6    # physical pin 31

# Placeholder calibration values — MUST be tuned on real hardware by running
# `python -m sensors.load_cell` with known reference weights (see README
# bring-up order).
HX711_REFERENCE_UNIT: float = 1.0  # raw-count-to-grams divisor, placeholder
HX711_ZERO_OFFSET: int = 0         # raw reading with an empty scale, placeholder

# Weight thresholds (grams).
WEIGHT_TRIGGER_G: float = 15.0          # crossing this wakes IDLE -> MEASURING
WEIGHT_STABLE_TOLERANCE_G: float = 1.5  # max spread across samples to call it "stable"
WEIGHT_STABLE_SAMPLES: int = 8
WEIGHT_STABLE_MAX_ATTEMPTS: int = 20    # give up waiting for a stable reading after this many sample batches

# Mock load cell baseline: a random weight generated near this value.
# Kept smaller than WEIGHT_STABLE_TOLERANCE_G so stable_reading() settles
# normally in mock demos instead of always hitting its give-up path.
MOCK_WEIGHT_BASELINE_G: float = 45.0
MOCK_WEIGHT_JITTER_G: float = 0.5

# --- Inductive metal sensor wiring -----------------------------------------
# NPN normally-open, active-low (LOW = metal detected). 10k pull-up to 3.3V.
METAL_SENSOR_PIN: int = 27  # physical pin 13
METAL_SENSOR_DEBOUNCE_S: float = 0.05
METAL_SENSOR_DEBOUNCE_SAMPLES: int = 3

# Mock metal sensor: probability a given poll reports metal present.
MOCK_METAL_TRIGGER_PROBABILITY: float = 0.5

# --- Camera ------------------------------------------------------------
CAMERA_INDEX: int = 0
CAMERA_JPEG_QUALITY: int = 90
MOCK_CAMERA_PLACEHOLDER_JPEG: Path = Path(__file__).resolve().parent / "vision" / "assets" / "placeholder.jpg"

# --- Servos (feetech-servo-sdk / st3215) ------------------------------------
SERVO_PORT: str = os.getenv("SERVO_PORT", "/dev/ttyACM0")
SERVO_BAUDRATE: int = 1_000_000
SERVO_ID_SAFE_GATE: int = 1
SERVO_ID_FLAGGED_GATE: int = 2

# Position placeholders (STS3215 range is 0-4095 for a 360-degree servo).
# TUNE THESE on real hardware during bring-up.
SERVO_HOME_POSITION: int = 2048
SERVO_SAFE_POSITION: int = 1024
SERVO_FLAGGED_POSITION: int = 3072
SERVO_MOVE_SPEED: int = 800
SERVO_MOVE_SETTLE_S: float = 1.0

# --- Logging -----------------------------------------------------------
LOG_DIR: Path = Path(__file__).resolve().parent / "logs"
LOG_FILE: Path = LOG_DIR / "sorter.log"
LOG_MAX_BYTES: int = 1_000_000
LOG_BACKUP_COUNT: int = 3

# --- State machine timing -----------------------------------------------
RESETTING_PAUSE_S: float = 1.5  # settle time before returning to IDLE
IDLE_POLL_INTERVAL_S: float = 0.1
