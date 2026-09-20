"""Plastic sorter state machine.

    SENSING -> (detected) -> ACTUATING -> HOLDING -> RESETTING -> SENSING
       '------ (nothing detected: keep sensing) ------'

There is no load cell yet, so nothing waits on weight: the loop senses the
whole time. Each SENSING round reads the metal sensor and classifies the
camera frame for plastic. If something sortable is detected (metal, or Gemini
says plastic) it tilts the bed to min/max, HOLDS for a few seconds, then
returns to level and resumes sensing. With no metal and no plastic the bed
stays at level. Sensing never runs while the servos are moving or holding —
the states are sequential.

Every state transition and sensor/decision value is logged, with timestamps,
to stdout and a rotating log file. Each step is wrapped in try/except so a bad
frame or dropped API call forces a return to level instead of hanging.

Run with MOCK_HARDWARE=1 (the default) to exercise the loop on a laptop with
no hardware attached; `--cycles N` stops after N tilts (handy for a bounded
mock demo).
"""
from __future__ import annotations

import argparse
import enum
import logging
import logging.handlers
import time

import config
from actuators.servo_pair import ServoPair
from logic.decision import decide_side
from sensors.metal_sensor import MetalSensor
from vision.camera import capture_frame
from vision.classifier import classify_material

log = logging.getLogger("sorter")


class State(enum.Enum):
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
        self.state = State.SENSING
        self.metal_sensor = MetalSensor()
        self.pair = ServoPair((config.SERVO_LEADER_ID, config.SERVO_FOLLOWER_ID))

        # Populated across a single pass through the pipeline.
        self.metal_present: bool = False
        self.classification: dict = {}
        self.plastic: bool = False
        self.side: str | None = None
        self.cycles_done: int = 0

        log.info("State machine initialized (MOCK_HARDWARE=%s)", config.MOCK_HARDWARE)
        self.pair.go_level()

    def _transition(self, new_state: State) -> None:
        log.info("transition: %s -> %s", self.state.value, new_state.value)
        self.state = new_state

    def _reset_item_state(self) -> None:
        self.metal_present = False
        self.classification = {}
        self.plastic = False
        self.side = None

    def close(self) -> None:
        """Release the servo bus (torque off) and the sensor. Safe to call twice."""
        try:
            self.pair.close()
        finally:
            self.metal_sensor.close()

    # -- state handlers -----------------------------------------------------
    def _handle_sensing(self) -> None:
        """Read both sensors once. Tilt if something is detected, else keep sensing."""
        self.metal_present = self.metal_sensor.is_metal_present()
        self.classification = classify_material(capture_frame())
        self.plastic = bool(self.classification.get("plastic", False))
        self.side = decide_side(self.plastic, self.metal_present)
        log.info(
            "sensed: metal_present=%s plastic=%s -> side=%s (classification=%s)",
            self.metal_present,
            self.plastic,
            self.side,
            self.classification,
        )
        if self.side is None:
            # Nothing sortable on the bed: stay level, sample again shortly.
            time.sleep(config.SENSE_INTERVAL_S)
            return
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
        self._reset_item_state()
        self.cycles_done += 1
        self._transition(State.SENSING)

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
