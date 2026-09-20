"""Plastic sorter state machine.

    WAITING -> (weight jumps up) -> SENSING -> ACTUATING -> HOLDING -> RESETTING -> WAITING
       '------------- (no jump yet: keep polling) --------------'

The load cell gates the whole pipeline: nothing is captured until the weight
RISES by more than WEIGHT_DELTA_TRIGGER_G (10 g) above the resting level measured
while waiting. It is the change that counts, not the absolute reading, so the
scale's zero never has to be right. On a trigger, SENSING lets the weight settle,
then reads the metal sensor and classifies the camera frame for plastic, and
the servo bed tilts (metal detected -> battery side; no metal -> non-battery side),
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
from logic.decision import describe, sort_side
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
        self._baseline_g: float | None = None   # resting weight; measured on entering WAITING

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
    def _measure_resting_weight(self) -> float:
        """The bed's resting weight, taken as soon as it has actually settled: the mean of the first
        WEIGHT_REST_SAMPLES consecutive readings that agree to within WEIGHT_BASELINE_BAND_G.
        Usually ~0.3 s; a bed still bouncing from the dump just takes a few readings longer."""
        if self.load_cell.mock:
            # The mock cell reports a constant "item" weight and never changes, so its resting
            # level is taken as 0 - that keeps the mock demo firing every loop, as before.
            return 0.0
        recent: list[float] = []
        for _ in range(config.WEIGHT_REST_MAX_SAMPLES):
            recent.append(self.load_cell.read_weight_g())
            recent = recent[-config.WEIGHT_REST_SAMPLES:]
            if len(recent) == config.WEIGHT_REST_SAMPLES and max(recent) - min(recent) <= config.WEIGHT_BASELINE_BAND_G:
                break
        else:
            log.warning("scale did not settle within %d readings; using the last %d", config.WEIGHT_REST_MAX_SAMPLES, len(recent))
        return sum(recent) / len(recent)

    def _handle_waiting(self) -> None:
        """Poll the load cell; a RISE of more than WEIGHT_DELTA_TRIGGER_G means an item was placed."""
        if self._baseline_g is None:
            self._baseline_g = self._measure_resting_weight()
            log.info("waiting: resting weight %.1fg; will start on a rise of more than %.1fg",
                     self._baseline_g, config.WEIGHT_DELTA_TRIGGER_G)

        weight = self.load_cell.read_weight_g(samples=2)
        delta = weight - self._baseline_g
        log.debug("waiting: weight=%.2fg baseline=%.2fg delta=%+.2fg", weight, self._baseline_g, delta)

        if delta > config.WEIGHT_DELTA_TRIGGER_G:
            log.info("weight jumped %+.1fg (%.1fg -> %.1fg), more than %.1fg: starting",
                     delta, self._baseline_g, weight, config.WEIGHT_DELTA_TRIGGER_G)
            self._transition(State.SENSING)
            return
        if delta < -config.WEIGHT_DELTA_TRIGGER_G:
            # Something was taken OFF the bed: that is the new resting level, not a trigger.
            log.info("weight dropped %+.1fg: new resting weight %.1fg", delta, weight)
            self._baseline_g = weight
        elif abs(delta) <= config.WEIGHT_BASELINE_BAND_G:
            # Slow drift (temperature, creep): let the resting level follow it.
            self._baseline_g += config.WEIGHT_BASELINE_TRACKING * delta
        time.sleep(config.IDLE_POLL_INTERVAL_S)

    def _handle_sensing(self) -> None:
        """Item confirmed present by weight: read metal + classify, then decide."""
        # Let the item settle before the photo. Its weight is what it ADDED to the resting level.
        self.weight_g = self.load_cell.stable_reading() - (self._baseline_g or 0.0)
        self.metal_present = self.metal_sensor.is_metal_present()
        self.classification = classify_material(capture_frame())
        self.plastic = bool(self.classification.get("plastic", False))
        self.side = sort_side(self.plastic, self.metal_present)
        log.info(
            "sensed: weight=%.2fg metal_present=%s plastic=%s -> side=%s [%s] (classification=%s)",
            self.weight_g,
            self.metal_present,
            self.plastic,
            self.side,
            describe(self.plastic, self.metal_present),
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
        # No re-tare here: the trigger is a RISE above the resting weight, and that resting
        # weight is measured afresh the moment we are back in WAITING, so the scale's zero
        # never matters. (A 15-sample tare cost ~1.3 s every single cycle.)
        self._reset_item_state()
        self._baseline_g = None    # the bed just emptied: measure the resting weight afresh
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
