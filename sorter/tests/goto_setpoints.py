"""PHYSICAL test: drive the bed to the recorded setpoints - level, min, max.

Uses the calibration saved by read_positions.py (zero / min / max / save). Each
servo goes to its OWN recorded position for that setpoint, so the two facing
servos turn opposite ways and the bed tilts as one - no invert flag needed.
Not a pytest test (no test_ prefix). Run it by hand:

    python goto_setpoints.py                 # from tests/: level -> min -> level -> max -> level
    python goto_setpoints.py max             # just go to one setpoint:  level | min | max
    python goto_setpoints.py min max min     # any sequence
    python goto_setpoints.py --speed 400 --hold-s 2
    python goto_setpoints.py max --margin 80  # stop 80 ticks (~7 deg) short of the recorded end
    python -m tests.goto_setpoints           # same thing, from sorter/

It prints the plan with the exact ticks for both servos and waits for Enter
before anything moves. Slow by default; torque limit from config.SERVO_TORQUE_LIMIT. Ends at the
last setpoint in the sequence, then releases torque (add --hold to keep holding).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# So this runs both as `python -m tests.goto_setpoints` from sorter/ and as `python goto_setpoints.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from actuators.calibration import SETPOINTS, Calibration
from actuators.servo_pair import ServoPair
from actuators.sts_bus import StsBusError

IDS = (43, 13)
DEFAULT_SEQUENCE = ["level", "min", "level", "max", "level"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python goto_setpoints.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sequence", nargs="*", metavar="SETPOINT",
                    help=f"setpoints to visit in order, from {', '.join(SETPOINTS)} (default: {' '.join(DEFAULT_SEQUENCE)})")
    ap.add_argument("--speed", type=int, default=250, help="steps/s (default 250 = ~22 deg/s)")
    ap.add_argument("--torque", type=int, default=config.SERVO_TORQUE_LIMIT,
                    help=f"torque limit 0-1000 (default {config.SERVO_TORQUE_LIMIT}, from config.SERVO_TORQUE_LIMIT)")
    ap.add_argument("--margin", type=int, default=None,
                    help="ticks to stop short of the recorded min/max (default: config.SERVO_SETPOINT_MARGIN). "
                         "Bigger = safer / less tilt. Can't go below the servo-limit margin.")
    ap.add_argument("--hold-s", type=float, default=1.0, help="pause at each setpoint (default 1.0 s)")
    ap.add_argument("--hold", action="store_true", help="keep torque on at the end instead of releasing")
    ap.add_argument("--yes", action="store_true", help="don't wait for Enter")
    args = ap.parse_args(argv)
    sequence = [name.lower() for name in args.sequence] or DEFAULT_SEQUENCE
    unknown = [name for name in sequence if name not in SETPOINTS]
    if unknown:
        print(f"Unknown setpoint(s) {unknown} - use: {', '.join(SETPOINTS)}")
        return 2

    cal = Calibration.load()
    print(cal.describe())
    try:
        plan = [(name, tuple(cal[sid].setpoint(name, args.margin) for sid in IDS)) for name in sequence]
    except (KeyError, ValueError) as e:
        print(f"\nCan't build the plan: {e}")
        return 2
    if cal.problems():
        return 2

    print(f"\nPlan for servos {IDS} at {args.speed} steps/s, torque limit {args.torque / 10:.0f}%:")
    for name, targets in plan:
        print(f"  {name:<6} -> servo {IDS[0]} = {targets[0]}, servo {IDS[1]} = {targets[1]}")
    if not args.yes:
        try:
            input("Bed clear? Press Enter to move, Ctrl+C to abort... ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted - nothing moved.")
            return 130

    pair = None
    try:
        pair = ServoPair(IDS, torque_limit=args.torque, mock=False, calibration=cal)
        print(f"start: {pair.positions()}  (relative to level: {pair.positions_rel()})")
        failed = []
        for name, targets in plan:
            arrived = pair.go_to(name, margin=args.margin, speed=args.speed)
            at, rel = pair.positions(), pair.positions_rel()
            print(f"  {name:<6} {'ok  ' if arrived else 'NOT REACHED'}  at {at}  relative {rel}  (wanted {targets})")
            if not arrived:
                failed.append(name)
                break  # don't keep pushing a mechanism that didn't get where it was told
            time.sleep(args.hold_s)
        if failed:
            print(f"\nFAIL: did not reach {failed[0]!r} - blocked, torque limit too low, or the servo's stored "
                  "limits stop short of it (python -m actuators.calibration apply).")
            return 1
        print("\nOK: every setpoint reached.")
        return 0
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except (StsBusError, RuntimeError, OSError) as e:
        print(f"\n{type(e).__name__}: {e}")
        return 1
    finally:
        if pair is not None:
            if args.hold:
                print("Torque left ON (--hold).")
                pair._bus.close()
            else:
                pair.close()
                print("Torque off.")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
