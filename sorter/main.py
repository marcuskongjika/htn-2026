"""Plastic sorter state machine.

    WAITING -> (weight sensed) -> SENSING -> ACTUATING -> HOLDING -> RESETTING -> WAITING
       '------------- (below trigger: keep polling) -------------'

The load cell gates the whole pipeline: nothing is captured until an item's
weight crosses WEIGHT_TRIGGER_G. On a trigger, SENSING lets the weight settle,
then reads the metal sensor and classifies the camera frame for plastic, and
the servo bed tilts (plastic with no metal -> min; everything else -> max),
HOLDS for a few seconds, returns to level, and waits for the next item.
Sensing never runs while the servos are moving or holding — the states are
sequential.

Every state transition and sensor/decision value is logged, with timestamps,
to stdout and a rotating log file. Each step is wrapped in try/except so a bad
frame or dropped API call forces a return to level instead of hanging.

Run with MOCK_HARDWARE=1 (the default) to exercise the loop on a laptop with
no hardware attached (the mock load cell's baseline weight sits above the
trigger, so it fires every loop); `--cycles N` stops after N cycles.
"""
from __future__ import annotations

import argparse
import enum
import logging
import logging.handlers
import time

import config
from actuators.servo_pair import ServoPair
from logic.decision import sort_side
from sensors.load_cell import LoadCell
from sensors.metal_sensor import MetalSensor
from vision.camera import capture_frame
from vision.classifier import classify_material

log = logging.getLogger("sorter")


class State(enum.Enum):
    WAITING = "WAITING"
    SENSING = "SENSING"
    ACTUATING = "ACTUATING"
    HOLDING = "HOLDING"
    RESETTING = "RESETTING"


def _configure_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


class SorterStateMachine:
    def __init__(self) -> None:
        self.state = State.WAITING
        self.load_cell = LoadCell()
        self.metal_sensor = MetalSensor()
        self.pair = ServoPair((config.SERVO_LEADER_ID, config.SERVO_FOLLOWER_ID))

        # Populated across a single pass through the pipeline.
        self.weight_g: float = 0.0
        self.metal_present: bool = False
        self.classification: dict = {}
        self.plastic: bool = False
        self.side: str | None = None
        self.cycles_done: int = 0

        log.info("State machine initialized (MOCK_HARDWARE=%s)", config.MOCK_HARDWARE)
        # Tare the empty scale on real hardware only; the mock stays un-tared so
        # its baseline weight keeps crossing the trigger for demos.
        if not config.MOCK_HARDWARE:
            self.load_cell.tare()
        self.pair.go_level()

    def _transition(self, new_state: State) -> None:
        log.info("transition: %s -> %s", self.state.value, new_state.value)
        self.state = new_state

    def _reset_item_state(self) -> None:
        self.weight_g = 0.0
        self.metal_present = False
        self.classification = {}
        self.plastic = False
        self.side = None

    def close(self) -> None:
        """Release the servo bus (torque off) and the sensors. Safe to call twice."""
        try:
            self.pair.close()
        finally:
            self.metal_sensor.close()
            self.load_cell.close()

    # -- state handlers -----------------------------------------------------
    def _handle_waiting(self) -> None:
        """Poll the load cell; a weight over the trigger means an item was placed."""
        weight = self.load_cell.read_weight_g(samples=3)
        log.debug("waiting: weight=%.2fg (trigger=%.2fg)", weight, config.WEIGHT_TRIGGER_G)
        if weight >= config.WEIGHT_TRIGGER_G:
            log.info("weight trigger crossed: %.2fg >= %.2fg", weight, config.WEIGHT_TRIGGER_G)
            self._transition(State.SENSING)
        else:
            time.sleep(config.IDLE_POLL_INTERVAL_S)

    def _handle_sensing(self) -> None:
        """Item confirmed present by weight: read metal + classify, then decide."""
        self.weight_g = self.load_cell.stable_reading()  # let the item settle before the photo
        self.metal_present = self.metal_sensor.is_metal_present()
        self.classification = classify_material(capture_frame())
        self.plastic = bool(self.classification.get("plastic", False))
        self.side = sort_side(self.plastic, self.metal_present)
        log.info(
            "sensed: weight=%.2fg metal_present=%s plastic=%s -> side=%s (classification=%s)",
            self.weight_g,
            self.metal_present,
            self.plastic,
            self.side,
            self.classification,
        )
        self._transition(State.ACTUATING)

    def _handle_actuating(self) -> None:
        arrived = self.pair.go_to(self.side)
        log.info("actuated: side=%s arrived=%s", self.side, arrived)
        self._transition(State.HOLDING)

    def _handle_holding(self) -> None:
        log.info("holding at %s for %.1fs", self.side, config.TILT_HOLD_S)
        time.sleep(config.TILT_HOLD_S)
        self._transition(State.RESETTING)

    def _handle_resetting(self) -> None:
        self.pair.go_level()
        time.sleep(config.RESETTING_PAUSE_S)
        # The bed is empty after the dump: re-zero the drift on real hardware
        # (skip in mock so the baseline weight keeps triggering).
        if not config.MOCK_HARDWARE:
            self.load_cell.tare()
        self._reset_item_state()
        self.cycles_done += 1
        self._transition(State.WAITING)

    _HANDLERS = None  # populated after class body, see below

    def step(self) -> None:
        handler = self._HANDLERS[self.state]
        handler(self)

    def run(self, max_cycles: int | None = None) -> None:
        """Sense/tilt forever, or until `max_cycles` tilts have completed.

        A single bad frame or dropped API call is caught and forces a return to
        level (RESETTING) rather than crashing or hanging the loop.
        """
        log.info("Entering sensing loop (max_cycles=%s)", max_cycles)
        while max_cycles is None or self.cycles_done < max_cycles:
            try:
                self.step()
            except Exception:
                log.exception(
                    "unhandled error in state %s; returning to level",
                    self.state.value,
                )
                self._transition(State.RESETTING)


SorterStateMachine._HANDLERS = {
    State.WAITING: SorterStateMachine._handle_waiting,
    State.SENSING: SorterStateMachine._handle_sensing,
    State.ACTUATING: SorterStateMachine._handle_actuating,
    State.HOLDING: SorterStateMachine._handle_holding,
    State.RESETTING: SorterStateMachine._handle_resetting,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous plastic/metal sorter loop.")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after this many completed tilt cycles (default: run until Ctrl+C)")
    args = parser.parse_args()

    _configure_logging()
    machine = SorterStateMachine()
    try:
        machine.run(max_cycles=args.cycles)
    finally:
        machine.close()


if __name__ == "__main__":
    main()
