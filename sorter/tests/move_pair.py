"""Hardware test: move BOTH servos together, the same amount, and bring them back.

Not a pytest test (the name doesn't start with test_, so pytest never runs it
and nothing moves by accident). Run it by hand from sorter/:

    python -m tests.move_pair                    # 43 and 13 both +100 ticks, then back
    python -m tests.move_pair --invert-13        # 43 goes +100, 13 goes -100 (mirrored pair)
    python -m tests.move_pair --ticks -200       # other way / further
    python -m tests.move_pair --stay --hold      # stay at the target with torque on

The two servos are one leader and one follower driving the same mechanism. If
they face each other across the bed, "the same direction" for the bed means
OPPOSITE directions for the servos - that is what --invert-13 is for. Find out
which you need with the horns free before anything is bolted together: both
horns should turn the same way as seen from one side of the bed.

Both goals go out in ONE sync-write packet, so the servos start in the same
instant rather than one after the other. Moves are relative to wherever each
servo is now, slow, and at a low torque limit. It asks before moving.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# So this runs both as `python -m ...` from sorter/ and directly as `python <file>.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from actuators.sts_bus import ADDR_MODE, ADDR_TORQUE_LIMIT, POSITION_MAX, TICKS_PER_REV, StsBus, StsBusError

LEADER_ID = 43
FOLLOWER_ID = 13
ARRIVED_TICKS = 8
MOVE_TIMEOUT_S = 5.0


def go_together(bus: StsBus, targets: dict[int, int], speed: int, acc: int) -> bool:
    """One sync write, then watch every servo until all have arrived. True if they all did."""
    bus.sync_write_pos_ex([(sid, pos, speed, acc) for sid, pos in targets.items()])
    deadline = time.monotonic() + MOVE_TIMEOUT_S
    while time.monotonic() < deadline:
        states = {sid: bus.feedback(sid) for sid in targets}
        line = "   ".join(f"[{sid}] pos {fb['position']:5d} load {fb['load']:+6.1f}% {fb['voltage']:.1f}V"
                          for sid, fb in states.items())
        print(f"\r  {line}   ", end="", flush=True)
        if all(abs(fb["position"] - targets[sid]) <= ARRIVED_TICKS and not fb["moving"] for sid, fb in states.items()):
            print()
            return True
        time.sleep(0.05)
    print()
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.move_pair", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leader", type=int, default=LEADER_ID, help=f"default {LEADER_ID}")
    ap.add_argument("--follower", type=int, default=FOLLOWER_ID, help=f"default {FOLLOWER_ID}")
    ap.add_argument("--invert-13", "--invert", dest="invert", action="store_true",
                    help="the follower (13) moves the opposite way to the leader")
    ap.add_argument("--ticks", type=int, default=100, help="how far; negative reverses (default 100 = ~9 deg)")
    ap.add_argument("--speed", type=int, default=200, help="steps/s (default 200 = ~18 deg/s)")
    ap.add_argument("--acc", type=int, default=10, help="x100 steps/s^2 (default 10)")
    ap.add_argument("--torque", type=int, default=300, help="torque limit 0-1000 (default 300 = 30%%)")
    ap.add_argument("--stay", action="store_true", help="don't move back to the start afterwards")
    ap.add_argument("--hold", action="store_true", help="leave torque on at the end")
    ap.add_argument("--yes", action="store_true", help="don't wait for Enter")
    ap.add_argument("--port", default=config.SERVO_PORT)
    args = ap.parse_args(argv)

    if abs(args.ticks) > 1024:
        print("More than 90 degrees in one go - refusing. Work up to it in smaller steps.")
        return 2
    if args.leader == args.follower:
        print("Leader and follower are the same ID.")
        return 2

    try:
        bus = StsBus(args.port, config.SERVO_BAUDRATE)
    except OSError as e:
        print(f"Could not open {args.port}: {e}")
        return 1

    deltas = {args.leader: args.ticks, args.follower: -args.ticks if args.invert else args.ticks}
    torque_on: list[int] = []
    try:
        missing = [sid for sid in deltas if not bus.ping(sid)]
        if missing:
            print(f"Servo(s) {missing} did not answer. Try: python -m actuators.sts_bus scan --all")
            return 1

        starts: dict[int, int] = {}
        targets: dict[int, int] = {}
        for sid, delta in deltas.items():
            fb = bus.feedback(sid)
            lower, upper = bus.angle_limits(sid)
            mode = bus.read_byte(sid, ADDR_MODE)
            role = "leader  " if sid == args.leader else "follower"
            target = fb["position"] + delta
            print(f"{role} {sid}: pos {fb['position']} -> {target} ({delta:+d} ticks, {delta * 360 / TICKS_PER_REV:+.1f} deg) | "
                  f"{fb['voltage']:.1f} V, {fb['temperature']} C, limits [{lower}, {upper}], errors {fb['errors'] or 'none'}")

            if mode != 0:
                print(f"  servo {sid} is in mode {mode}, not position mode. Not moving.")
                return 2
            if fb["errors"]:
                print(f"  servo {sid} is reporting errors. Not moving.")
                return 2
            if not 0 <= target <= POSITION_MAX:
                print(f"  target {target} is off the end of the 0-{POSITION_MAX} range. Try a smaller or opposite --ticks.")
                return 2
            if not (lower == 0 and upper == 0) and not (lower <= target <= upper):
                print(f"  servo {sid} only accepts goals in [{lower}, {upper}] and would clamp this one. "
                      f"Reset its limits first: python -m tools.nudge {sid} --open-limits")
                return 2
            starts[sid], targets[sid] = fb["position"], target

        print(f"\nBoth start together at {args.speed} steps/s, torque limit {args.torque / 10:.0f}%"
              + ("" if args.stay else ", then both return"))
        if not args.yes:
            input("Horns free / mechanism clear? Press Enter to move, Ctrl+C to abort... ")

        for sid in deltas:
            bus.write_word(sid, ADDR_TORQUE_LIMIT, args.torque)
            bus.set_torque(sid, True)
            torque_on.append(sid)

        print("Moving...")
        arrived = go_together(bus, targets, args.speed, args.acc)
        print("Both reached target." if arrived else "NOT both at target (blocked, fighting each other, torque limit too low, or supply sagging?).")

        if not args.stay:
            time.sleep(0.3)
            print("Returning...")
            go_together(bus, starts, args.speed, args.acc)

        for sid in deltas:
            print(f"servo {sid}: final {bus.feedback(sid)['position']} (started {starts[sid]})")
        return 0 if arrived else 1
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except StsBusError as e:
        print(f"\n{type(e).__name__}: {e}")
        return 1
    finally:
        if not args.hold:
            for sid in torque_on:
                try:
                    bus.set_torque(sid, False)
                except StsBusError:
                    pass
            if torque_on:
                print("Torque off.")
        bus.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
