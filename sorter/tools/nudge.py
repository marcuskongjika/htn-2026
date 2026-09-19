"""First-motion test: move one servo a little, slowly, and bring it back.

    cd sorter
    python -m tools.nudge 43                  # +100 ticks (about 9 deg), then back
    python -m tools.nudge 43 --ticks -150     # other direction
    python -m tools.nudge 43 --stay           # don't return to the start
    python -m tools.nudge 43 --open-limits    # see "Angle limits" below

What it does, in order - and it stops to ask before anything moves:

    1. ping the servo, print position / voltage / temperature / errors
    2. read the servo's stored angle limits and work out the target
    3. refuse if the servo would not end up where we asked (see below)
    4. wait for Enter
    5. low torque limit, torque on, ONE slow move, watch it arrive
    6. move back to the start, torque off

Angle limits: the servo keeps a min/max goal position in EEPROM and silently
clamps any goal outside it. A servo that came off another robot still carries
that robot's joint limits, so "+100 ticks" can turn into a swing all the way
to the nearest limit. This script refuses instead. --open-limits rewrites the
limits to the full 0-4095 range first (one small EEPROM write).

Run it with the horn free - nothing attached to the servo.
"""
from __future__ import annotations

import argparse
import sys
import time

import config
from actuators.sts_bus import ADDR_MODE, ADDR_TORQUE_LIMIT, POSITION_MAX, TICKS_PER_REV, StsBus, StsBusError

ARRIVED_TICKS = 8          # within ~0.7 deg counts as there
MOVE_TIMEOUT_S = 4.0


def degrees(ticks: int) -> float:
    return ticks * 360 / TICKS_PER_REV


def go(bus: StsBus, servo_id: int, target: int, speed: int, acc: int) -> bool:
    """Send one goal and watch the servo get there. True if it arrived."""
    bus.write_pos_ex(servo_id, target, speed, acc)
    deadline = time.monotonic() + MOVE_TIMEOUT_S
    while time.monotonic() < deadline:
        fb = bus.feedback(servo_id)
        print(f"\r  pos {fb['position']:5d}  load {fb['load']:+6.1f}%  {fb['current_ma']:6.0f} mA   ", end="", flush=True)
        if abs(fb["position"] - target) <= ARRIVED_TICKS and not fb["moving"]:
            print()
            return True
        time.sleep(0.05)
    print()
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.nudge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("servo_id", type=int)
    ap.add_argument("--ticks", type=int, default=100, help="how far to move; negative reverses (default 100 = ~9 deg)")
    ap.add_argument("--speed", type=int, default=200, help="steps/s (default 200 = ~18 deg/s)")
    ap.add_argument("--acc", type=int, default=10, help="x100 steps/s^2 (default 10)")
    ap.add_argument("--torque", type=int, default=300, help="torque limit 0-1000 (default 300 = 30%%)")
    ap.add_argument("--stay", action="store_true", help="don't move back to the start afterwards")
    ap.add_argument("--hold", action="store_true", help="leave torque on at the end")
    ap.add_argument("--open-limits", action="store_true", help="rewrite the servo's angle limits to 0-4095 if they are in the way")
    ap.add_argument("--yes", action="store_true", help="don't wait for Enter")
    ap.add_argument("--port", default=config.SERVO_PORT)
    args = ap.parse_args(argv)

    if abs(args.ticks) > 512:
        print("That's more than 45 degrees - not a nudge. Use `python -m actuators.sts_bus move` for big moves.")
        return 2

    try:
        bus = StsBus(args.port, config.SERVO_BAUDRATE)
    except OSError as e:
        print(f"Could not open {args.port}: {e}")
        return 1

    sid = args.servo_id
    torque_on = False
    try:
        if not bus.ping(sid):
            print(f"Servo {sid} did not answer. Try: python -m actuators.sts_bus scan --all")
            return 1

        fb = bus.feedback(sid)
        start = fb["position"]
        lower, upper = bus.angle_limits(sid)
        mode = bus.read_byte(sid, ADDR_MODE)
        print(f"Servo {sid}: pos {start} ({degrees(start):.1f} deg), {fb['voltage']:.1f} V, {fb['temperature']} C, "
              f"mode {mode}, limits [{lower}, {upper}], errors {fb['errors'] or 'none'}")

        if mode != 0:
            print(f"Servo is in mode {mode}, not position mode (0). Not moving it.")
            return 2
        if fb["errors"]:
            print("Servo is reporting errors. Not moving it.")
            return 2

        target = start + args.ticks
        if not 0 <= target <= POSITION_MAX:
            print(f"Target {target} is off the end of the 0-{POSITION_MAX} range. Try --ticks {-args.ticks}.")
            return 2

        limited = not (lower == 0 and upper == 0)  # 0/0 means "no limits" on these servos
        if limited and not (lower <= start <= upper and lower <= target <= upper):
            clamped = min(max(target, lower), upper)
            print(f"\nThe servo only accepts goals in [{lower}, {upper}] and would clamp {target} to {clamped}: "
                  f"a swing of {abs(clamped - start)} ticks ({degrees(abs(clamped - start)):.0f} deg), not {abs(args.ticks)}.")
            if not args.open_limits:
                print("Not moving. Re-run with --open-limits to reset the limits to 0-4095 first.")
                return 2
            print("--open-limits given: the limits will be rewritten to [0, 4095] before the move.")

        print(f"\nPlan: {start} -> {target} ({degrees(args.ticks):+.1f} deg) at {args.speed} steps/s, "
              f"torque limit {args.torque / 10:.0f}%" + ("" if args.stay else f", then back to {start}"))
        if not args.yes:
            input("Horn free? Press Enter to move, Ctrl+C to abort... ")

        if limited and args.open_limits:
            bus.set_angle_limits(sid, 0, POSITION_MAX)
            print(f"Angle limits now {bus.angle_limits(sid)}")

        bus.write_word(sid, ADDR_TORQUE_LIMIT, args.torque)
        bus.set_torque(sid, True)
        torque_on = True

        print("Moving...")
        arrived = go(bus, sid, target, args.speed, args.acc)
        print("Reached target." if arrived else "Did NOT reach target (blocked, torque limit too low, or supply sagging?).")

        if not args.stay:
            time.sleep(0.3)
            print("Returning...")
            go(bus, sid, start, args.speed, args.acc)

        print(f"Final position {bus.feedback(sid)['position']} (started at {start}).")
        return 0 if arrived else 1
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except StsBusError as e:
        print(f"\n{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            if torque_on and not args.hold:
                bus.set_torque(sid, False)
                print("Torque off.")
        except StsBusError:
            pass
        bus.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
