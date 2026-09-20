"""Two servos driven as one: a leader and a follower on the same bus.

    from actuators.servo_pair import ServoPair

    with ServoPair((43, 13), invert=(False, True)) as pair:
        pair.move_by(300)            # POSITION mode: both go +300 ticks (13 mirrored), wait, hold
        pair.move_by(-300)

    with ServoPair((43, 13)) as pair:    # no invert given: zero, direction and travel come from the
        pair.go_level()                  # stored calibration (actuators/calibration.py)
        pair.go_max()                    # tilt to the recorded "max" end; go_min() for the other
        pair.move_rel(+150)              # or anywhere between: 150 ticks from LEVEL toward "max"

        pair.run_for(2.0, speed=400) # WHEEL mode: both spin for 2 s, then stop

`invert` flips a servo's direction. Two servos facing each other across a
mechanism must turn opposite ways to move it one way - that pair is
invert=(False, True).

The two modes are different things, pick by mechanism:

  move_by / move_to   The servo goes to an angle and stops there by itself.
                      Right for anything with limited travel (a tilting bed).
  run_for             The servo spins like a motor until told to stop. It has
                      NO idea where the end stops are: on limited travel it
                      drives into the frame until the time is up. Right for
                      rollers, belts, wheels.

run_for always stops both servos and puts them back in position mode, even if
the program is interrupted or the bus throws. Every command that starts motion
goes out as one sync-write packet, so the pair starts in the same instant.

CLI (from sorter/):
    python -m actuators.servo_pair move 43 13 --ticks 200 --invert-second
    python -m actuators.servo_pair run  43 13 --seconds 2 --speed 300 --invert-second
    python -m actuators.servo_pair goto 43 13 --to max        # level | min | max, from the calibration
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from actuators.calibration import Calibration
from actuators.sts_bus import (
    ADDR_MODE,
    ADDR_TORQUE_LIMIT,
    MODE_POSITION,
    MODE_WHEEL,
    POSITION_MAX,
    StsBus,
    StsBusError,
)

log = logging.getLogger(__name__)

# Module-level so the tests can fast-forward this class's clock without touching the bus layer's.
_monotonic = time.monotonic
_sleep = time.sleep

ARRIVED_TICKS = 8
MAX_WHEEL_SPEED = 3400  # steps/s, about the servo's no-load ceiling


class ServoPair:
    def __init__(
        self,
        ids: tuple[int, int],
        invert: tuple[bool, bool] | None = None,
        torque_limit: int | None = None,
        bus: StsBus | None = None,
        mock: bool | None = None,
        calibration: Calibration | None = None,
    ) -> None:
        """ids: (leader, follower). torque_limit: 0-1000, applied to both; default config.SERVO_TORQUE_LIMIT.
        invert: leave as None to take each servo's direction from the stored calibration
        (no calibration = neither inverted). calibration: defaults to the global file."""
        if ids[0] == ids[1]:
            raise ValueError("the two servo IDs must be different")
        self.ids = tuple(ids)
        self.calibration = Calibration.load() if calibration is None else calibration
        if invert is None:
            invert = tuple(self.calibration[sid].invert if sid in self.calibration else False for sid in self.ids)
        self._sign = {ids[0]: -1 if invert[0] else 1, ids[1]: -1 if invert[1] else 1}
        self.mock = config.MOCK_HARDWARE if mock is None else mock
        self._owns_bus = bus is None
        self._bus = bus

        if self.mock:
            log.info("ServoPair %s running in MOCK mode", self.ids)
            return

        if self._bus is None:
            self._bus = StsBus(config.SERVO_PORT, config.SERVO_BAUDRATE)
        missing = [sid for sid in self.ids if not self._bus.ping(sid)]
        if missing:
            if self._owns_bus:
                self._bus.close()
            raise RuntimeError(f"servo ID(s) {missing} did not answer on {config.SERVO_PORT}")
        torque_limit = config.SERVO_TORQUE_LIMIT if torque_limit is None else torque_limit
        for sid in self.ids:
            self._bus.write_word(sid, ADDR_TORQUE_LIMIT, torque_limit)
        self._enter_position_mode()

    # --- position mode -----------------------------------------------------------
    def positions(self) -> tuple[int, int]:
        if self.mock:
            return (config.SERVO_HOME_POSITION, config.SERVO_HOME_POSITION)
        return tuple(self._bus.feedback(sid)["position"] for sid in self.ids)

    def move_to(self, targets: tuple[int, int], speed: int | None = None, acc: int | None = None,
                wait: bool = True, timeout_s: float = 6.0) -> bool:
        """Absolute positions (leader, follower), 0-4095. Returns True if both arrived
        (always True with wait=False). The servos hold the position afterwards."""
        speed = config.SERVO_MOVE_SPEED if speed is None else speed
        acc = config.SERVO_MOVE_ACC if acc is None else acc
        for target in targets:
            if not 0 <= target <= POSITION_MAX:
                raise ValueError(f"target {target} is outside 0-{POSITION_MAX}")
        if self.mock:
            log.info("[MOCK] pair %s -> positions %s", self.ids, tuple(targets))
            return True

        self._bus.sync_write_pos_ex([(sid, target, speed, acc) for sid, target in zip(self.ids, targets)])
        if not wait:
            return True
        deadline = _monotonic() + timeout_s
        while _monotonic() < deadline:
            states = [self._bus.feedback(sid) for sid in self.ids]
            if all(abs(fb["position"] - t) <= ARRIVED_TICKS and not fb["moving"] for fb, t in zip(states, targets)):
                return True
            _sleep(0.03)
        log.warning("pair %s did not reach %s within %.1f s (at %s)", self.ids, tuple(targets), timeout_s, self.positions())
        return False

    def move_by(self, ticks: int, **kwargs) -> bool:
        """Relative move: the mechanism goes `ticks` one way; an inverted servo turns the other way."""
        now = self.positions()
        return self.move_to(tuple(pos + self._sign[sid] * ticks for sid, pos in zip(self.ids, now)), **kwargs)

    # --- calibrated (mechanism) coordinates -----------------------------------------
    def move_rel(self, rel: int, **kwargs) -> bool:
        """Go to `rel` ticks from LEVEL (positive = toward the end recorded as "max"). Each
        servo's zero, direction and measured travel come from the stored calibration, so this
        means the same thing whatever angle the horns were bolted on at."""
        return self.move_to(tuple(self.calibration[sid].to_abs(rel) for sid in self.ids), **kwargs)

    def go_to(self, name: str, margin: int | None = None, **kwargs) -> bool:
        """Go to a recorded setpoint: "level", "min" or "max". Both servos start together, each
        heading for its OWN recorded position at that end (so a mirrored servo turns the other
        way and the bed tilts as one). Returns True when both have arrived."""
        # margin: ticks inside the recorded end; default config.SERVO_SETPOINT_MARGIN
        targets = tuple(self.calibration[sid].setpoint(name, margin) for sid in self.ids)
        log.info("pair %s -> %s %s", self.ids, name, targets)
        return self.move_to(targets, **kwargs)

    def go_level(self, **kwargs) -> bool:
        return self.go_to("level", **kwargs)

    def go_min(self, **kwargs) -> bool:
        return self.go_to("min", **kwargs)

    def go_max(self, **kwargs) -> bool:
        return self.go_to("max", **kwargs)

    def home(self, **kwargs) -> bool:
        """Level."""
        return self.go_level(**kwargs)

    def positions_rel(self) -> tuple[int, int]:
        return tuple(self.calibration[sid].to_rel(pos) for sid, pos in zip(self.ids, self.positions()))

    # --- wheel mode --------------------------------------------------------------
    def run_for(self, seconds: float, speed: int = 300, acc: int = 20, on_poll=None) -> None:
        """Spin both for `seconds` at `speed` steps/s (negative = the other way), then stop.
        See the module docstring: only for mechanisms that can turn forever.

        on_poll: optional callback, called ~20x a second while spinning with
        {servo_id: feedback dict} - for live readouts and logging."""
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        if abs(speed) > MAX_WHEEL_SPEED:
            raise ValueError(f"speed must be within +/-{MAX_WHEEL_SPEED} steps/s")
        if self.mock:
            log.info("[MOCK] pair %s spinning at %d steps/s for %.2f s", self.ids, speed, seconds)
            return

        try:
            for sid in self.ids:
                self._bus.set_torque(sid, False)
                self._bus.write_byte(sid, ADDR_MODE, MODE_WHEEL)
                self._bus.set_torque(sid, True)
            self._bus.sync_write_speed([(sid, self._sign[sid] * speed, acc) for sid in self.ids])
            log.info("pair %s spinning at %d steps/s for %.2f s", self.ids, speed, seconds)
            deadline = _monotonic() + seconds
            while _monotonic() < deadline:
                states = {sid: self._bus.feedback(sid) for sid in self.ids}  # watch them, don't sleep blind
                for sid, fb in states.items():
                    if fb["errors"]:
                        raise StsBusError(f"servo {sid} reported {fb['errors']} while spinning - stopped early")
                if on_poll is not None:
                    on_poll(states)
                _sleep(min(0.05, max(0.0, deadline - _monotonic())))
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop spinning and return both servos to position mode, holding where they are."""
        if self.mock:
            return
        try:
            self._bus.sync_write_speed([(sid, 0, 0) for sid in self.ids])
        finally:
            self._enter_position_mode()

    # --- housekeeping --------------------------------------------------------------
    def _enter_position_mode(self) -> None:
        """Torque off -> position mode -> goal = where it is now -> torque on, so
        switching modes can never make a servo jump to a stale goal."""
        for sid in self.ids:
            self._bus.set_torque(sid, False)
            self._bus.write_byte(sid, ADDR_MODE, MODE_POSITION)
            here = self._bus.feedback(sid)["position"]
            self._bus.write_pos_ex(sid, min(max(here, 0), POSITION_MAX), config.SERVO_MOVE_SPEED, config.SERVO_MOVE_ACC)
            self._bus.set_torque(sid, True)

    def release(self) -> None:
        """Torque off: both servos go limp."""
        if self.mock:
            return
        for sid in self.ids:
            self._bus.set_torque(sid, False)

    def close(self) -> None:
        if self.mock or self._bus is None:
            return
        try:
            self.stop()
            self.release()
        finally:
            if self._owns_bus:
                self._bus.close()
            self._bus = None

    def __enter__(self) -> "ServoPair":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m actuators.servo_pair", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("move", "run", "goto"):
        p = sub.add_parser(name)
        p.add_argument("leader", type=int)
        p.add_argument("follower", type=int)
        p.add_argument("--invert-first", action="store_true")
        p.add_argument("--invert-second", action="store_true")
        p.add_argument("--torque", type=int, default=config.SERVO_TORQUE_LIMIT,
                       help=f"torque limit 0-1000 (default {config.SERVO_TORQUE_LIMIT}, from config.SERVO_TORQUE_LIMIT)")
        p.add_argument("--speed", type=int, default=300, help="steps/s (default 300)")
        p.add_argument("--yes", action="store_true", help="don't wait for Enter")
        if name == "move":
            p.add_argument("--ticks", type=int, default=100)
        elif name == "run":
            p.add_argument("--seconds", type=float, default=1.0)
        else:
            p.add_argument("--to", choices=["level", "min", "max"], required=True)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    what = {"move": lambda: f"move {args.ticks:+d} ticks",
            "run": lambda: f"SPIN for {args.seconds} s (no end stops!)",
            "goto": lambda: f"go to the recorded {args.to!r} setpoint"}[args.cmd]()
    print(f"Servos {args.leader} and {args.follower}: {what} at {args.speed} steps/s, torque limit {args.torque / 10:.0f}%")
    if not args.yes:
        input("Mechanism clear? Press Enter to go, Ctrl+C to abort... ")
    try:
        # goto takes each servo's direction from the calibration; move/run use the flags.
        invert = None if args.cmd == "goto" else (args.invert_first, args.invert_second)
        with ServoPair((args.leader, args.follower), invert=invert, torque_limit=args.torque, mock=False) as pair:
            print("start positions:", pair.positions())
            if args.cmd == "move":
                print("arrived:", pair.move_by(args.ticks, speed=args.speed))
            elif args.cmd == "goto":
                print("arrived:", pair.go_to(args.to, speed=args.speed), "| relative to level:", pair.positions_rel())
            else:
                pair.run_for(args.seconds, speed=args.speed)
            print("end positions:  ", pair.positions())
        return 0
    except KeyboardInterrupt:
        print("\nAborted (servos stopped).")
        return 130
    except (StsBusError, RuntimeError, OSError, KeyError, ValueError) as e:
        print(f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
