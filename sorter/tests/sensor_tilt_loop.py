"""PHYSICAL test: metal sensor -> bed. Loop forever:

    hold LEVEL -> you place an item + press ENTER -> wait until GPIO17 gives one CONSISTENT
    answer -> tilt -> back to LEVEL -> repeat

    line 0 = metal     -> go_max()
    line 1 = no metal  -> go_min()          (--swap flips which side is which)

"Consistent" means every sample for a whole window (default 1 s, sampled every 20 ms)
agrees. A flickering sensor - an item at the edge of its range - never produces a
decision, and the bed just stays at level until the reading settles.

Each cycle waits for ENTER, because there is no weight sensor yet to say "an item has been
placed" - and the metal sensor can't tell: it rests at 1 (no metal), the same reading as a
plastic item. Without the Enter it would tilt an empty bed to the no-metal side forever.
(--auto does exactly that, for soak-testing the mechanism.)

Not a pytest test (no test_ prefix). Run it by hand:

    python sensor_tilt_loop.py                   # from tests/: Enter = "item placed", then read + tilt
    python sensor_tilt_loop.py --cycles 3        # stop after 3 tilts
    python sensor_tilt_loop.py --auto            # no Enter: read + tilt continuously
    python sensor_tilt_loop.py --swap            # metal -> min, no metal -> max
    python sensor_tilt_loop.py --window 2 --pause 3 --speed 200
    python -m tests.sensor_tilt_loop             # same thing, from sorter/

Asks before anything moves. At the item prompt, q + Enter finishes. Ctrl+C at any time:
back to level, then torque off.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# So this runs both as `python -m tests.sensor_tilt_loop` from sorter/ and as `python sensor_tilt_loop.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from actuators.calibration import Calibration
from actuators.servo_pair import ServoPair
from actuators.sts_bus import StsBusError
from sensors.metal_sensor import MetalSensor

IDS = (43, 13)

# Module-level so the simulated test can fast-forward them.
_monotonic = time.monotonic
_sleep = time.sleep


def wait_for_consistent(read, window_s: float, sample_s: float, on_sample=None) -> bool:
    """Block until `read()` has returned the same value for a whole window; return that value.
    Any disagreement restarts the window, so a flickering input never decides anything."""
    value = read()
    since = _monotonic()
    while True:
        if on_sample is not None:
            on_sample(value, _monotonic() - since)
        if _monotonic() - since >= window_s:
            return value
        _sleep(sample_s)
        now = read()
        if now != value:
            value, since = now, _monotonic()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python sensor_tilt_loop.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=float, default=1.0, help="seconds the reading must stay the same (default 1.0)")
    ap.add_argument("--sample", type=float, default=0.02, help="seconds between samples (default 0.02)")
    ap.add_argument("--hold-s", type=float, default=1.5, help="seconds to stay tilted so the item slides off (default 1.5)")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds at level before reading again (default 2.0)")
    ap.add_argument("--cycles", type=int, default=0, help="stop after this many tilts (default 0 = until Ctrl+C)")
    ap.add_argument("--swap", action="store_true", help="metal -> min and no metal -> max, instead of the other way round")
    ap.add_argument("--auto", action="store_true",
                    help="don't wait for Enter before each cycle: read and tilt continuously (tilts an empty bed too)")
    ap.add_argument("--speed", type=int, default=250, help="steps/s (default 250 = ~22 deg/s)")
    ap.add_argument("--margin", type=int, default=None, help="ticks short of the recorded min/max (default: config)")
    ap.add_argument("--torque", type=int, default=config.SERVO_TORQUE_LIMIT, help=f"torque limit 0-1000 (default {config.SERVO_TORQUE_LIMIT})")
    ap.add_argument("--yes", action="store_true", help="don't wait for Enter before starting")
    args = ap.parse_args(argv)

    cal = Calibration.load()
    side = {True: "min" if args.swap else "max", False: "max" if args.swap else "min"}  # key: metal?
    try:
        targets = {name: tuple(cal[sid].setpoint(name, args.margin) for sid in IDS) for name in ("level", "min", "max")}
    except (KeyError, ValueError) as e:
        print(f"Can't use the calibration: {e}")
        return 2

    print(f"GPIO{config.METAL_SENSOR_PIN} (internal pull-up): 0 = metal -> {side[True]} {targets[side[True]]},  "
          f"1 = no metal -> {side[False]} {targets[side[False]]},  level {targets['level']}")
    print(("AUTO: cycles run back to back. " if args.auto else "Each cycle waits for Enter. ")
          + f"Decision needs {args.window:g} s of identical samples. Tilt holds {args.hold_s:g} s, then {args.pause:g} s at level. "
          f"{args.speed} steps/s, torque limit {args.torque / 10:.0f}%.")
    if not args.yes:
        try:
            input("Bed clear? Press Enter to start, Ctrl+C to abort... ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted - nothing moved.")
            return 130

    sensor = pair = None
    tilts = {"min": 0, "max": 0}
    try:
        sensor = MetalSensor(mock=False)
        pair = ServoPair(IDS, torque_limit=args.torque, mock=False, calibration=cal)
        if not pair.go_level(speed=args.speed):
            print("Could not reach level - not starting the loop.")
            return 1

        def show(metal: bool, stable_s: float) -> None:
            print(f"\r  line {0 if metal else 1} ({'metal' if metal else 'no metal':8}) steady {min(stable_s, args.window):4.2f}/{args.window:g} s   ",
                  end="", flush=True)

        cycle = 0
        while not args.cycles or cycle < args.cycles:
            cycle += 1
            if not args.auto:
                try:
                    typed = input(f"\n[{cycle}] place the item, then press Enter  (q + Enter to finish)... ")
                except EOFError:
                    typed = "q"
                if typed.strip().lower() in ("q", "quit", "exit"):
                    break
            metal = wait_for_consistent(sensor.read, args.window, args.sample, on_sample=show)
            name = side[metal]
            print(f"\n[{cycle}] consistent {0 if metal else 1} = {'METAL' if metal else 'no metal'} -> {name}")

            arrived = pair.go_to(name, margin=args.margin, speed=args.speed)
            print(f"      {name}: {'reached' if arrived else 'NOT REACHED'} at {pair.positions()}  (relative {pair.positions_rel()})")
            tilts[name] += 1
            _sleep(args.hold_s)

            if not pair.go_level(speed=args.speed):
                print("      could not get back to level - stopping.")
                return 1
            if not arrived:
                print("      stopping: the tilt did not reach its setpoint (blocked, or torque limit too low).")
                return 1
            print(f"      level at {pair.positions()}")
            _sleep(args.pause)
        print(f"\nDone: {tilts['max']} x max, {tilts['min']} x min.")
        return 0
    except KeyboardInterrupt:
        print(f"\nStopped: {tilts['max']} x max, {tilts['min']} x min.")
        if pair is not None:
            try:
                pair.go_level(speed=args.speed)
            except StsBusError:
                pass
        return 130
    except (StsBusError, RuntimeError, OSError) as e:
        print(f"\n{type(e).__name__}: {e}")
        return 1
    finally:
        if pair is not None:
            pair.close()
            print("Torque off.")
        if sensor is not None:
            sensor.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
