"""PHYSICAL test: RUN both motors together for a set time, then run them back.

This one spins the servos like motors (wheel mode) - unlike move_pair.py, which
sends them to an angle. It really moves hardware, so it is not a pytest test
(no test_ prefix: pytest never collects it). Run it by hand:

    python run_pair.py                          # from tests/: 43 + 13, 1 s forward, pause, 1 s back
    python run_pair.py --invert-13              # 13 spins the opposite way to 43
    python run_pair.py --seconds 2 --speed 400
    python run_pair.py --reverse                # start in the other direction
    python run_pair.py --one-way                # forward only, don't run back
    python -m tests.run_pair                    # same thing, from sorter/

WHEEL MODE HAS NO END STOPS. The servos turn for the full time whatever is in
the way. First run: horns free, short, slow (the defaults). On a mechanism with
limited travel use move_pair.py instead.

What you get: a live line per servo (position, load, volts, amps), then how far
each one actually turned and whether the pair stayed in step. Both servos are
always stopped and put back in position mode at the end - also on Ctrl+C.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# So this runs both as `python -m tests.run_pair` from sorter/ and as `python run_pair.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators.servo_pair import ServoPair
from actuators.sts_bus import TICKS_PER_REV, StsBusError

LEADER_ID = 43
FOLLOWER_ID = 13


class TravelMeter:
    """Wheel-mode position wraps 4095 -> 0 every turn. This adds up the small steps
    between polls so we know the real distance turned, across wraps."""

    def __init__(self, ids):
        self.last = {sid: None for sid in ids}
        self.travel = {sid: 0 for sid in ids}
        self.peak_load = {sid: 0.0 for sid in ids}
        self.min_volts = {sid: 99.0 for sid in ids}

    def __call__(self, states: dict[int, dict]) -> None:
        parts = []
        for sid, fb in states.items():
            pos = fb["position"] % TICKS_PER_REV
            if self.last[sid] is not None:
                step = (pos - self.last[sid] + TICKS_PER_REV // 2) % TICKS_PER_REV - TICKS_PER_REV // 2
                self.travel[sid] += step
            self.last[sid] = pos
            self.peak_load[sid] = max(self.peak_load[sid], abs(fb["load"]))
            self.min_volts[sid] = min(self.min_volts[sid], fb["voltage"])
            parts.append(f"[{sid}] pos {pos:4d} load {fb['load']:+6.1f}% {fb['voltage']:.1f}V {fb['current_ma']:5.0f}mA")
        print("\r  " + "   ".join(parts) + "   ", end="", flush=True)


def spin(pair: ServoPair, label: str, seconds: float, speed: int, acc: int) -> TravelMeter:
    meter = TravelMeter(pair.ids)
    print(f"{label}: {seconds:g} s at {speed:+d} steps/s")
    pair.run_for(seconds, speed=speed, acc=acc, on_poll=meter)
    print()
    for sid in pair.ids:
        t = meter.travel[sid]
        print(f"  servo {sid}: turned {t:+d} ticks ({t * 360 / TICKS_PER_REV:+.0f} deg), "
              f"peak load {meter.peak_load[sid]:.0f}%, lowest supply {meter.min_volts[sid]:.1f} V")
    return meter


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python run_pair.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leader", type=int, default=LEADER_ID, help=f"default {LEADER_ID}")
    ap.add_argument("--follower", type=int, default=FOLLOWER_ID, help=f"default {FOLLOWER_ID}")
    ap.add_argument("--invert-13", "--invert", dest="invert", action="store_true",
                    help="the follower (13) spins the opposite way to the leader")
    ap.add_argument("--seconds", type=float, default=1.0, help="how long each run lasts (default 1.0)")
    ap.add_argument("--speed", type=int, default=200, help="steps/s (default 200 = ~18 deg/s; max ~3400)")
    ap.add_argument("--acc", type=int, default=20, help="x100 steps/s^2 ramp (default 20)")
    ap.add_argument("--torque", type=int, default=300, help="torque limit 0-1000 (default 300 = 30%%)")
    ap.add_argument("--reverse", action="store_true", help="first run goes the other way")
    ap.add_argument("--one-way", action="store_true", help="don't run back afterwards")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds stopped between the two runs")
    ap.add_argument("--yes", action="store_true", help="don't wait for Enter")
    args = ap.parse_args(argv)

    if args.seconds > 10:
        print("More than 10 s per run - refusing. This is a test, not a drive cycle.")
        return 2

    speed = -args.speed if args.reverse else args.speed
    expected = abs(args.speed) * args.seconds
    print(f"Servos {args.leader} (leader) + {args.follower} (follower{', INVERTED' if args.invert else ''}): "
          f"spin {args.seconds:g} s at {speed:+d} steps/s"
          + ("" if args.one_way else f", pause {args.pause:g} s, spin back")
          + f". Torque limit {args.torque / 10:.0f}%. Expect about {expected:.0f} ticks "
          f"({expected * 360 / TICKS_PER_REV:.0f} deg) each way.")
    print("Wheel mode: NO end stops. Horns free / mechanism able to turn that far?")
    if not args.yes:
        input("Press Enter to run, Ctrl+C to abort... ")

    try:
        with ServoPair((args.leader, args.follower), invert=(False, args.invert),
                       torque_limit=args.torque, mock=False) as pair:
            print(f"start positions: {pair.positions()}")
            out = spin(pair, "Forward" if not args.reverse else "Reverse", args.seconds, speed, args.acc)

            if not args.one_way:
                time.sleep(args.pause)
                spin(pair, "Back", args.seconds, -speed, args.acc)

            print(f"end positions:   {pair.positions()}")

            # Verdict on the first run: did both turn, and by about the same amount?
            a, b = (abs(out.travel[sid]) for sid in pair.ids)
            if min(a, b) < 0.3 * expected:
                print(f"\nFAIL: a servo barely turned ({a} and {b} ticks, expected ~{expected:.0f}). "
                      "Blocked, torque limit too low, or supply sagging?")
                return 1
            if abs(a - b) > 0.25 * max(a, b):
                print(f"\nWARN: the pair did not stay in step ({a} vs {b} ticks) - one is loaded more than the other.")
                return 1
            same_way = (out.travel[pair.ids[0]] > 0) == (out.travel[pair.ids[1]] > 0)
            print(f"\nOK: both turned ~{(a + b) // 2} ticks, in {'the SAME' if same_way else 'OPPOSITE'} raw direction"
                  f"{' (as asked: --invert-13)' if args.invert else ''}.")
            return 0
    except KeyboardInterrupt:
        print("\nAborted - servos stopped and back in position mode.")
        return 130
    except (StsBusError, RuntimeError, OSError) as e:
        print(f"\n{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
