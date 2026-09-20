"""PHYSICAL tool: read both servos' positions live while you move the mechanism BY HAND.

Used to find the mechanical limits. Nothing is commanded to move: torque is
switched OFF so the mechanism turns freely, and the script just reads.

    python read_positions.py                 # from tests/: servos 43 and 13
    python read_positions.py 43 13 7         # any IDs
    python read_positions.py --keep-torque   # read only, leave torque exactly as it is
    python -m tests.read_positions           # same thing, from sorter/

While it runs:
    - put the mechanism at your reference (level) and type  zero  + Enter: from then on every
      reading is ALSO shown relative to that spot, as +/- ticks. (These servos have one encoder,
      an absolute one; "relative" is just absolute minus a reference, which is what this does.)
    - move the mechanism to a spot you care about (one end stop, level, the other end stop)
    - type a name for it and press Enter  ->  that position is recorded   (e.g.  min   level   max)
    - type  save  + Enter: zero + the positions named min and max are written to the global
      calibration file (actuators/calibration.py) that the rest of the code loads
    - just Enter records it with a number instead of a name
    - q + Enter (or Ctrl+C) finishes and prints the table to send back

It also remembers the lowest and highest position each servo passed through, so
sweeping the mechanism from one end stop to the other captures the full travel
even if you record nothing.

CAREFUL: with torque off nothing holds the mechanism up. Support it before you
press Enter at the start.
"""
from __future__ import annotations

import argparse
import select
import sys
from pathlib import Path

# So this runs both as `python -m tests.read_positions` from sorter/ and as `python read_positions.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from actuators.calibration import Calibration
from actuators.sts_bus import TICKS_PER_REV, StsBus, StsBusError

DEFAULT_IDS = [43, 13]
WRAP_JUMP = TICKS_PER_REV // 2  # a bigger step than this between two reads means we crossed 4095 <-> 0


def degrees(ticks: int) -> float:
    return ticks * 360 / TICKS_PER_REV


def relative(position: int, zero: int) -> int:
    """Signed shortest distance from zero to position, correct across the 4095 <-> 0 boundary."""
    return (position - zero + TICKS_PER_REV // 2) % TICKS_PER_REV - TICKS_PER_REV // 2


class Recorder:
    """Keeps the named snapshots plus the lowest/highest position seen per servo."""

    def __init__(self, ids: list[int]) -> None:
        self.ids = list(ids)
        self.last: dict[int, int | None] = {sid: None for sid in ids}
        self.low: dict[int, int] = {}
        self.high: dict[int, int] = {}
        self.wrapped: set[int] = set()
        self.snapshots: list[tuple[str, dict[int, int]]] = []
        self.zero: dict[int, int] | None = None

    def set_zero(self) -> dict[int, int]:
        """Make the current positions the reference for relative readings."""
        self.zero = {sid: self.last[sid] for sid in self.ids}
        return self.zero

    def rel(self, sid: int, position: int) -> int | None:
        return None if self.zero is None else relative(position, self.zero[sid])

    def poll(self, positions: dict[int, int]) -> None:
        for sid, pos in positions.items():
            previous = self.last[sid]
            if previous is not None and abs(pos - previous) > WRAP_JUMP:
                self.wrapped.add(sid)
            self.last[sid] = pos
            self.low[sid] = min(self.low.get(sid, pos), pos)
            self.high[sid] = max(self.high.get(sid, pos), pos)

    def snapshot(self, label: str) -> tuple[str, dict[int, int]]:
        label = label or f"#{len(self.snapshots) + 1}"
        entry = (label, {sid: self.last[sid] for sid in self.ids})
        self.snapshots.append(entry)
        return entry

    def calibration(self) -> Calibration:
        """zero + the snapshots named min / max -> a Calibration. Raises ValueError if it can't."""
        if self.zero is None:
            raise ValueError("no zero yet - level the mechanism and type: zero")
        named = {label.lower(): positions for label, positions in self.snapshots}
        if self.wrapped:
            raise ValueError(f"servo(s) {sorted(self.wrapped)} crossed the 4095 <-> 0 boundary - can't store limits across it")
        cal = Calibration.from_measurements(self.zero, named.get("min"), named.get("max"))
        if cal.problems():
            raise ValueError("; ".join(cal.problems()))
        return cal

    def summary(self) -> str:
        width = max([8] + [len(label) for label, _ in self.snapshots])
        lines = ["", "=" * 60, "Send this back:", ""]
        def cell(sid: int, pos: int) -> str:
            r = self.rel(sid, pos)
            return f"{pos:>6}" + (f" ({r:+5d})" if r is not None else "")

        cell_width = 14 if self.zero is not None else 6
        lines.append(f"{'':{width}}  " + "  ".join(f"{'servo ' + str(sid):>{cell_width}}" for sid in self.ids))
        if self.zero is not None:
            lines.append(f"{'zero':{width}}  " + "  ".join(f"{self.zero[sid]:>6}{'':8}" for sid in self.ids))
        for label, positions in self.snapshots:
            lines.append(f"{label:{width}}  " + "  ".join(cell(sid, positions[sid]) for sid in self.ids))
        if not self.snapshots:
            lines.append("(no named positions recorded)")
        lines.append("")
        lines.append("absolute ticks" + (", with (relative to zero) in brackets" if self.zero is not None else "")
                     + ". Limits are stored on the servo in ABSOLUTE ticks.")
        lines.append("")
        lines.append("lowest / highest position each servo passed through:")
        for sid in self.ids:
            if sid in self.low:
                span = self.high[sid] - self.low[sid]
                lines.append(f"  servo {sid}: {self.low[sid]} .. {self.high[sid]}   (travel {span} ticks = {degrees(span):.0f} deg)")
        for sid in sorted(self.wrapped):
            lines.append(f"  !! servo {sid} crossed the 4095 <-> 0 boundary while moving. Its lowest/highest above are "
                         "NOT its real travel, and position limits cannot span that boundary - the horn needs "
                         "re-seating so the whole travel sits inside 0-4095 (ideally centred near 2048).")
        lines.append("=" * 60)
        return "\n".join(lines)


def _read_line(timeout_s: float) -> str | None:
    """A typed line if one is waiting, else None after timeout_s."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if not ready:
        return None
    line = sys.stdin.readline()
    return "q" if line == "" else line.strip()  # "" = stdin closed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python read_positions.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", type=int, nargs="*", default=DEFAULT_IDS, help=f"servo IDs (default {DEFAULT_IDS})")
    ap.add_argument("--keep-torque", action="store_true", help="don't switch torque off; just read")
    ap.add_argument("--yes", action="store_true", help="don't wait for Enter before releasing torque")
    ap.add_argument("--port", default=config.SERVO_PORT)
    args = ap.parse_args(argv)

    try:
        bus = StsBus(args.port, config.SERVO_BAUDRATE)
    except OSError as e:
        print(f"Could not open {args.port}: {e}")
        return 1

    recorder = Recorder(args.ids)
    try:
        missing = [sid for sid in args.ids if not bus.ping(sid)]
        if missing:
            print(f"Servo(s) {missing} did not answer. Try: python -m actuators.sts_bus scan --all")
            return 1

        for sid in args.ids:
            fb = bus.feedback(sid)
            lower, upper = bus.angle_limits(sid)
            print(f"servo {sid}: pos {fb['position']} ({degrees(fb['position']):.1f} deg), {fb['voltage']:.1f} V, "
                  f"stored limits [{lower}, {upper}], errors {fb['errors'] or 'none'}")

        if not args.keep_torque:
            if not args.yes:
                input("\nTorque will be switched OFF - support the mechanism. Press Enter to continue, Ctrl+C to abort... ")
            for sid in args.ids:
                bus.set_torque(sid, False)
            print("Torque off. Move it by hand.")
        print("zero = make this spot the reference | min / max (or any name) = record a position | save = store zero+min+max globally | q = finish\n")

        while True:
            positions = {sid: bus.feedback(sid)["position"] for sid in args.ids}
            recorder.poll(positions)
            def live(sid: int, pos: int) -> str:
                r = recorder.rel(sid, pos)
                return f"[{sid}] {pos:5d}" + (f"  rel {r:+5d} ({degrees(r):+6.1f} deg)" if r is not None else f" ({degrees(pos):6.1f} deg)")

            print("\r  " + "   ".join(live(sid, pos) for sid, pos in positions.items()) + "    > ", end="", flush=True)
            line = _read_line(0.1)
            if line is None:
                continue
            if line.lower() in ("q", "quit", "exit"):
                break
            if line.lower() == "save":
                try:
                    cal = recorder.calibration()
                    print(f"  saved -> {cal.save()}")
                    print("  " + cal.describe().replace("\n", "\n  "))
                    if "min" not in {label.lower() for label, _ in recorder.snapshots} or "max" not in {label.lower() for label, _ in recorder.snapshots}:
                        print("  (only the zero was stored - record positions named min and max, then save again, to store the end stops)")
                except ValueError as e:
                    print(f"  NOT saved: {e}")
                continue
            if line.lower() in ("zero", "z"):
                zero = recorder.set_zero()
                print("  zero set: " + ", ".join(f"servo {sid} = {pos}" for sid, pos in zero.items()) + "  (readings now also shown relative to this)")
                continue
            label, snap = recorder.snapshot(line)
            print(f"  recorded {label!r}: " + ", ".join(f"servo {sid} = {pos}" for sid, pos in snap.items()))
    except KeyboardInterrupt:
        print()
    except StsBusError as e:
        print(f"\n{type(e).__name__}: {e}")
        print(recorder.summary())
        return 1
    finally:
        bus.close()

    print(recorder.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
