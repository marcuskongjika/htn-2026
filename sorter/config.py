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

HX711_GAIN: int = 128     # channel A; 64 also valid. 128 is right for a bar load cell
# Tare + scale measured on the rig by `python tests/load_cell_read.py` (t = tare,
# c <grams> = calibrate with a known weight, save). When this file exists it wins over
# the two placeholder numbers below.
LOAD_CELL_CALIBRATION_FILE: Path = Path(os.getenv("LOAD_CELL_CALIBRATION_FILE", Path(__file__).resolve().parent / "load_cell_calibration.json"))

# Fallback calibration, used only if the calibration file above is missing. Measured on the
# rig 2026-09-20 (10 kg bar cell, gain 128): raw -194131 empty, -141857 with 250 g on
#   -> (-141857 - -194131) / 250 = 209.096 counts per gram.
# grams = (raw - HX711_ZERO_OFFSET) / HX711_REFERENCE_UNIT. The zero drifts with temperature
# and with anything bolted to the cell, so LoadCell.tare() at start-up; the counts/g does not.
HX711_REFERENCE_UNIT: float = 209.096
HX711_ZERO_OFFSET: int = -196640   # -194131 first; empty scale then read -12 g, so moved by -12 g = -2509 counts

# Weight thresholds (grams).
WEIGHT_TRIGGER_G: float = 15.0          # (old absolute trigger; main.py now uses the delta below)
# main.py starts a scan when the weight RISES by more than this above the resting level it
# measured while waiting. A change, not an absolute value: it does not matter where the
# scale's zero is, what is bolted to the cell, or how far the zero has drifted.
WEIGHT_DELTA_TRIGGER_G: float = float(os.getenv("WEIGHT_DELTA_TRIGGER_G", "10.0"))
# While waiting, the resting level follows slow drift: readings within this band of it are
# blended in (WEIGHT_BASELINE_TRACKING per poll). Anything bigger is a real change, not drift.
WEIGHT_BASELINE_BAND_G: float = 3.0
WEIGHT_BASELINE_TRACKING: float = 0.05
WEIGHT_STABLE_TOLERANCE_G: float = 1.5  # max spread across samples to call it "stable"
WEIGHT_STABLE_SAMPLES: int = 8
WEIGHT_STABLE_MAX_ATTEMPTS: int = 20    # give up waiting for a stable reading after this many sample batches

# Mock load cell baseline: a random weight generated near this value.
# Kept smaller than WEIGHT_STABLE_TOLERANCE_G so stable_reading() settles
# normally in mock demos instead of always hitting its give-up path.
MOCK_WEIGHT_BASELINE_G: float = 45.0
MOCK_WEIGHT_JITTER_G: float = 0.5

# --- Inductive metal sensor wiring -----------------------------------------
# LJ18A3-8-Z/BX: NPN, normally open, so its output only ever pulls the line to GND.
#   brown -> + supply (6-36 V)     blue -> GND, shared with the Pi     black -> the GPIO pin
# The Pi's INTERNAL pull-up holds the line at 3.3 V; metal present pulls it LOW. No external
# resistor. (On a Pi 5 this needs the lgpio backend - see requirements.txt.)
# Before wiring black to the Pi: meter black-to-blue with no metal near. It must NOT sit at
# the supply voltage - some clones pull it up internally, and that needs a divider first.
METAL_SENSOR_PIN: int = int(os.getenv("METAL_SENSOR_PIN", "17"))  # BCM 17 = physical pin 11
METAL_SENSOR_PULL_UP: bool = True
METAL_SENSOR_DEBOUNCE_S: float = 0.05
METAL_SENSOR_DEBOUNCE_SAMPLES: int = 3

# Mock metal sensor: probability a given poll reports metal present.
MOCK_METAL_TRIGGER_PROBABILITY: float = 0.5

# --- Camera ------------------------------------------------------------
CAMERA_INDEX: int = 0
CAMERA_JPEG_QUALITY: int = 90
MOCK_CAMERA_PLACEHOLDER_JPEG: Path = Path(__file__).resolve().parent / "vision" / "assets" / "placeholder.jpg"

# --- Servos (STS3215, driven directly from the Pi's UART) -------------------
# No bus adapter board: the header UART talks to the servo's one-wire data
# line, and actuators/sts_bus.py handles the half-duplex framing.
#   GPIO14 TX (physical pin 8) --[1k]--+
#   GPIO15 RX (physical pin 10) -------+---- servo DATA
# /dev/ttyAMA0 is the header UART on a Pi 5 (needs `dtparam=uart0=on` in
# /boot/firmware/config.txt). To go back to a USB bus adapter, set
# SERVO_PORT=/dev/ttyACM0 in .env - nothing else changes.
SERVO_PORT: str = os.getenv("SERVO_PORT", "/dev/ttyAMA0")
SERVO_BAUDRATE: int = 1_000_000
SERVO_ID_SAFE_GATE: int = int(os.getenv("SERVO_ID_SAFE_GATE", "1"))
SERVO_ID_FLAGGED_GATE: int = int(os.getenv("SERVO_ID_FLAGGED_GATE", "2"))

# The two servos that tilt the sorting bed as a leader/follower pair, matching
# the IDs in servo_calibration.json. main.py drives these through ServoPair.
SERVO_LEADER_ID: int = int(os.getenv("SERVO_LEADER_ID", "43"))
SERVO_FOLLOWER_ID: int = int(os.getenv("SERVO_FOLLOWER_ID", "13"))

# Position placeholders (STS3215 range is 0-4095 for a 360-degree servo).
# TUNE THESE on real hardware during bring-up.
SERVO_HOME_POSITION: int = 2048
SERVO_SAFE_POSITION: int = 1024
SERVO_FLAGGED_POSITION: int = 3072
SERVO_MOVE_SPEED: int = 800   # steps/s (4096 steps = one turn); 0 would mean "no cap"
SERVO_MOVE_ACC: int = 50      # x100 steps/s^2; 0 would mean "no ramp"
SERVO_MOVE_SETTLE_S: float = 1.0

# Per-servo zero (level) position, end stops and mirroring, measured on the rig by
# tests/read_positions.py and used everywhere through actuators/calibration.py.
# Torque limit for moving the bed, 0-1000 (600 = 60% of what the servo can give). 30% was too
# little to reach the setpoints with the bed attached - and on a 2S pack (~8 V) the servos only
# have about two thirds of their rated 12 V torque to begin with. Raise it if moves come up
# short ("NOT REACHED"); lower it if you want the bed to give way more easily when blocked.
SERVO_TORQUE_LIMIT: int = int(os.getenv("SERVO_TORQUE_LIMIT", "600"))

# The two bins, by what goes in them. The decision logic only ever says "battery" or
# "non_battery"; THIS is the one place that says which physical end of the bed each one is.
# Swapped the bins over? Change "max" to "min" here (or set BATTERY_SIDE_SETPOINT in .env).
BATTERY_SIDE: str = "battery"            # metal detected, or not clean plastic: could hide a battery
NON_BATTERY_SIDE: str = "non_battery"    # plastic with no metal: safe
BATTERY_SIDE_SETPOINT: str = os.getenv("BATTERY_SIDE_SETPOINT", "max").lower()   # "max" or "min"
if BATTERY_SIDE_SETPOINT not in ("min", "max"):
    raise ValueError(f"BATTERY_SIDE_SETPOINT must be 'min' or 'max', not {BATTERY_SIDE_SETPOINT!r}")
SIDE_SETPOINTS: dict[str, str] = {
    BATTERY_SIDE: BATTERY_SIDE_SETPOINT,
    NON_BATTERY_SIDE: "min" if BATTERY_SIDE_SETPOINT == "max" else "max",
}

# Safety margins, in ticks (11.4 ticks = 1 degree), measured inward from the RECORDED min/max:
#   limit margin    -> where the servo's own EEPROM limits go (python -m actuators.calibration apply)
#   setpoint margin -> where go_min()/go_max() actually stop, and the furthest move_rel() will go
# The setpoint margin must be the larger one: the gap between them is room to overshoot a
# setpoint without ever reaching the servo's limit, let alone the hard stop.
SERVO_LIMIT_MARGIN: int = int(os.getenv("SERVO_LIMIT_MARGIN", "25"))        # ~2 deg
SERVO_SETPOINT_MARGIN: int = int(os.getenv("SERVO_SETPOINT_MARGIN", "50"))  # ~4.4 deg
SERVO_CALIBRATION_FILE: Path = Path(os.getenv("SERVO_CALIBRATION_FILE", Path(__file__).resolve().parent / "servo_calibration.json"))

# --- Logging -----------------------------------------------------------
LOG_DIR: Path = Path(__file__).resolve().parent / "logs"
LOG_FILE: Path = LOG_DIR / "sorter.log"
LOG_MAX_BYTES: int = 1_000_000
LOG_BACKUP_COUNT: int = 3

# --- State machine timing -----------------------------------------------
RESETTING_PAUSE_S: float = 1.5  # settle time before returning to IDLE
IDLE_POLL_INTERVAL_S: float = 0.1

# --- Sorting loop -------------------------------------------------------
# Env-overridable so the mock demo can use a short hold.
TILT_HOLD_S: float = float(os.getenv("TILT_HOLD_S", "5.0"))  # hold at min/max before returning to level
