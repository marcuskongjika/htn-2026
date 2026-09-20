"""Battery-sorter state machine.

    IDLE -> MEASURING -> CLASSIFYING -> DECIDING -> ACTUATING -> RESETTING -> IDLE

Runs as a simple sequential loop (no async/threading — items are processed
one at a time). Every state transition and every sensor/decision value is
logged, with timestamps, to both stdout and a rotating log file. The loop
body is wrapped in try/except so a single bad frame or dropped API call
logs the error and forces a transition to RESETTING instead of crashing or
hanging the whole pipeline.

Run with MOCK_HARDWARE=1 (the default, see config.py / .env.example) to
exercise the full loop on a laptop with no hardware attached.
"""
from __future__ import annotations

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
    IDLE = "IDLE"
    MEASURING = "MEASURING"
    CLASSIFYING = "CLASSIFYING"
    DECIDING = "DECIDING"
    ACTUATING = "ACTUATING"
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
        self.state = State.IDLE
        self.load_cell = LoadCell()
        self.metal_sensor = MetalSensor()
        self.pair = ServoPair((config.SERVO_LEADER_ID, config.SERVO_FOLLOWER_ID))

        # Populated across a single pass through the pipeline.
        self.weight_g: float = 0.0
        self.metal_present: bool = False
        self.classification: dict = {}
        self.plastic: bool = False
        self.side: str | None = None

        log.info("State machine initialized (MOCK_HARDWARE=%s)", config.MOCK_HARDWARE)
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
        """Release the servo bus (torque off) and the sensor. Safe to call twice."""
        try:
            self.pair.close()
        finally:
            self.metal_sensor.close()

    # -- state handlers -----------------------------------------------------
    def _handle_idle(self) -> None:
        weight = self.load_cell.read_weight_g()
        log.debug("idle poll: weight=%.2fg (trigger=%.2fg)", weight, config.WEIGHT_TRIGGER_G)
        if weight >= config.WEIGHT_TRIGGER_G:
            log.info("weight trigger crossed: %.2fg >= %.2fg", weight, config.WEIGHT_TRIGGER_G)
            self._transition(State.MEASURING)
        else:
            time.sleep(config.IDLE_POLL_INTERVAL_S)

    def _handle_measuring(self) -> None:
        self.weight_g = self.load_cell.stable_reading()
        self.metal_present = self.metal_sensor.is_metal_present()
        log.info(
            "measured: weight=%.2fg metal_present=%s", self.weight_g, self.metal_present
        )
        self._transition(State.CLASSIFYING)

    def _handle_classifying(self) -> None:
        frame = capture_frame()
        self.classification = classify_material(frame)
        log.info("classified: %s", self.classification)
        self._transition(State.DECIDING)

    def _handle_deciding(self) -> None:
        self.plastic = bool(self.classification.get("plastic", False))
        self.side = sort_side(self.plastic, self.metal_present)
        log.info(
            "decision: side=%s (plastic=%s metal_present=%s classification=%s)",
            self.side,
            self.plastic,
            self.metal_present,
            self.classification,
        )
        self._transition(State.ACTUATING)

    def _handle_actuating(self) -> None:
        arrived = self.pair.go_to(self.side)
        log.info("actuated: side=%s arrived=%s", self.side, arrived)
        self._transition(State.RESETTING)

    def _handle_resetting(self) -> None:
        time.sleep(config.RESETTING_PAUSE_S)
        self.pair.go_level()
        self._reset_item_state()
        self._transition(State.IDLE)

    _HANDLERS = None  # populated after class body, see below

    def step(self) -> None:
        handler = self._HANDLERS[self.state]
        handler(self)

    def run_forever(self) -> None:
        log.info("Entering main loop")
        while True:
            try:
                self.step()
            except Exception:
                log.exception(
                    "unhandled error in state %s; forcing transition to RESETTING",
                    self.state.value,
                )
                self._transition(State.RESETTING)

    def run_one_cycle(self) -> None:
        """Run one full pass: wait for the IDLE weight trigger, then proceed
        through MEASURING -> ... -> RESETTING back to IDLE.

        Used for smoke-testing the full pipeline in mock mode without an
        infinite loop (see README's mock bring-up instructions). The mock
        load cell's baseline weight sits above WEIGHT_TRIGGER_G, so IDLE
        triggers on its first poll rather than being skipped.
        """
        log.info("Running a single full cycle (mock smoke test)")
        while self.state != State.MEASURING:
            self.step()
        while self.state != State.RESETTING:
            self.step()
        final_side = self.side  # snapshot before RESETTING clears item state
        self.step()  # RESETTING -> IDLE
        log.info("Cycle complete: side=%s", final_side)


SorterStateMachine._HANDLERS = {
    State.IDLE: SorterStateMachine._handle_idle,
    State.MEASURING: SorterStateMachine._handle_measuring,
    State.CLASSIFYING: SorterStateMachine._handle_classifying,
    State.DECIDING: SorterStateMachine._handle_deciding,
    State.ACTUATING: SorterStateMachine._handle_actuating,
    State.RESETTING: SorterStateMachine._handle_resetting,
}


def main() -> None:
    _configure_logging()
    machine = SorterStateMachine()

    try:
        if config.MOCK_HARDWARE:
            # In mock mode there's no real weight event to wait on forever, so
            # run one deterministic end-to-end cycle and exit — this is what the
            # README's "run with MOCK_HARDWARE=1" acceptance check exercises.
            machine.run_one_cycle()
            return

        machine.run_forever()
    finally:
        machine.close()


if __name__ == "__main__":
    main()
