"""Plastic sorter, Enter-triggered: the same pipeline as main.py WITHOUT the load cell.

    WAITING -> (you press Enter) -> SENSING -> ACTUATING -> HOLDING -> RESETTING -> WAITING

main.py waits for the load cell to see an item's weight. This variant never opens
the HX711 at all - no weight trigger, no settling read, no taring - so a flaky or
unwired load cell can't stall or crash the run. You place the item, press Enter,
and from there it is exactly main.py: read the metal sensor, classify the camera
frame for plastic, tilt (metal detected -> battery side; no metal -> non-battery side),
hold, return to level, and wait for the next Enter.

    python main_enter.py               # Enter = scan the item on the bed;  q + Enter = quit
    python main_enter.py --cycles 5    # stop after 5 items
    python main_enter.py --auto        # no prompt: scan back to back (soak test / mock demo)

Everything else (logging, mock mode, config, error handling that returns the bed
to level) is inherited from main.py, so the two cannot drift apart.
"""
from __future__ import annotations

import argparse
import logging
import time

import config
from actuators.servo_pair import ServoPair
from logic.decision import describe, sort_side
from main import SorterStateMachine, State, _configure_logging
from sensors.metal_sensor import MetalSensor
from vision.camera import capture_frame
from vision.classifier import classify_material

log = logging.getLogger("sorter")


class EnterSorterStateMachine(SorterStateMachine):
    """SorterStateMachine with the load cell taken out and an Enter prompt put in its place."""

    def __init__(self, auto: bool = False) -> None:
        # Deliberately NOT calling super().__init__(): that is what opens (and tares) the load cell.
        self.state = State.WAITING
        self.auto = auto
        self.stop_requested = False
        self.load_cell = None
        self.metal_sensor = MetalSensor()
        self.pair = ServoPair((config.SERVO_LEADER_ID, config.SERVO_FOLLOWER_ID))

        self.weight_g: float = 0.0          # stays 0: nothing weighs the item in this variant
        self.metal_present: bool = False
        self.classification: dict = {}
        self.plastic: bool = False
        self.side: str | None = None
        self.cycles_done: int = 0

        log.info("Enter-triggered state machine initialized (MOCK_HARDWARE=%s, load cell NOT used)", config.MOCK_HARDWARE)
        self.pair.go_level()

    def close(self) -> None:
        """Level the bed, then release the servo bus (torque off) and the metal sensor."""
        try:
            try:
                self.pair.go_level()
            except Exception:
                log.exception("could not return to level while shutting down")
            self.pair.close()
        finally:
            self.metal_sensor.close()

    # -- state handlers -----------------------------------------------------
    def _handle_waiting(self) -> None:
        """Enter stands in for the weight trigger: it means 'an item is on the bed'."""
        if self.auto:
            self._transition(State.SENSING)
            return
        try:
            typed = input(f"\n[{self.cycles_done + 1}] place the item, then press Enter  (q + Enter to quit)... ")
        except EOFError:
            typed = "q"
        if typed.strip().lower() in ("q", "quit", "exit"):
            self.stop_requested = True
            return
        self._transition(State.SENSING)

    def _handle_sensing(self) -> None:
        """Read metal + classify, then decide. No weight involved."""
        self.metal_present = self.metal_sensor.is_metal_present()
        self.classification = classify_material(capture_frame())
        self.plastic = bool(self.classification.get("plastic", False))
        self.side = sort_side(self.plastic, self.metal_present)
        log.info(
            "sensed: metal_present=%s plastic=%s -> side=%s [%s] (classification=%s)",
            self.metal_present,
            self.plastic,
            self.side,
            describe(self.plastic, self.metal_present),
            self.classification,
        )
        self._transition(State.ACTUATING)

    def _handle_resetting(self) -> None:
        self.pair.go_level()
        time.sleep(config.RESETTING_PAUSE_S)
        self._reset_item_state()
        self.cycles_done += 1
        self._transition(State.WAITING)

    def run(self, max_cycles: int | None = None) -> None:
        """Scan/tilt on each Enter until q, Ctrl+C, or `max_cycles` tilts have completed."""
        log.info("Entering Enter-triggered loop (max_cycles=%s, auto=%s)", max_cycles, self.auto)
        while not self.stop_requested and (max_cycles is None or self.cycles_done < max_cycles):
            try:
                self.step()
            except Exception:
                log.exception("unhandled error in state %s; returning to level", self.state.value)
                self._transition(State.RESETTING)
        log.info("Stopping after %d item(s)", self.cycles_done)


# ACTUATING and HOLDING are main.py's own handlers, unchanged.
EnterSorterStateMachine._HANDLERS = {
    **SorterStateMachine._HANDLERS,
    State.WAITING: EnterSorterStateMachine._handle_waiting,
    State.SENSING: EnterSorterStateMachine._handle_sensing,
    State.RESETTING: EnterSorterStateMachine._handle_resetting,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Plastic/metal sorter loop, started per item with Enter (no load cell).")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after this many completed tilt cycles (default: run until q or Ctrl+C)")
    parser.add_argument("--auto", action="store_true",
                        help="don't wait for Enter: scan and tilt back to back")
    args = parser.parse_args()

    _configure_logging()
    machine = EnterSorterStateMachine(auto=args.auto)
    try:
        machine.run(max_cycles=args.cycles)
    except KeyboardInterrupt:
        log.info("Ctrl+C - levelling and shutting down")
    finally:
        machine.close()


if __name__ == "__main__":
    main()
