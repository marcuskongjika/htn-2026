"""Servo calibration: each servo's zero (level) position, its mechanical end
stops, and whether it is mirrored - measured once on the rig, stored in one
JSON file, used everywhere.

The file (config.SERVO_CALIBRATION_FILE, default sorter/servo_calibration.json):

    {"servos": {"43": {"zero": 1786, "lower": 1490, "upper": 2080, "invert": false},
                "13": {"zero": 3660, "lower": 3365, "upper": 3955, "invert": true}}}

It is written by `python tests/read_positions.py` (type zero / min / max, then
save). Everything else reads it:

    from actuators.calibration import Calibration
    cal = Calibration.load()
    cal[43].to_abs(+300)     # mechanism coordinate -> this servo's absolute ticks
    cal[43].to_rel(2086)     # and back

Mechanism coordinates are ticks from level, positive toward the end that was
recorded as "max". A mirrored servo (invert) turns the other way for the same
mechanism move, so its absolute ticks go down as the coordinate goes up.

    python -m actuators.calibration show     # print the stored calibration
    python -m actuators.calibration apply    # write lower/upper into each servo's EEPROM limits
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

log = logging.getLogger(__name__)

POSITION_MAX = 4095
SETPOINTS = ("level", "min", "max", config.BATTERY_SIDE, config.NON_BATTERY_SIDE)


def limit_margin() -> int:
    return config.SERVO_LIMIT_MARGIN


def setpoint_margin() -> int:
    """Never smaller than the limit margin: a setpoint past the servo's limit can't be reached."""
    return max(config.SERVO_SETPOINT_MARGIN, config.SERVO_LIMIT_MARGIN)


@dataclass
class ServoCal:
    zero: int
    lower: int | None = None   # absolute ticks; None = end stops not measured yet
    upper: int | None = None
    invert: bool = False

    @property
    def sign(self) -> int:
        return -1 if self.invert else 1

    def bounds(self, margin: int = 0) -> tuple[int, int]:
        lower = 0 if self.lower is None else self.lower + margin
        upper = POSITION_MAX if self.upper is None else self.upper - margin
        return lower, upper

    def to_abs(self, rel: int, margin: int | None = None) -> int:
        """Mechanism coordinate -> absolute ticks, held inside the SAFE travel (the recorded
        ends minus the setpoint margin), so no relative move can go past go_min()/go_max()."""
        wanted = self.zero + self.sign * int(rel)
        lower, upper = self.bounds(setpoint_margin() if margin is None else margin)
        clamped = min(max(wanted, lower), upper)
        if clamped != wanted:
            log.warning("relative target %+d (= %d) is outside the safe travel [%d, %d]; using %d",
                        rel, wanted, lower, upper, clamped)
        return clamped

    def setpoint(self, name: str, margin: int | None = None) -> int:
        """Absolute ticks for a named spot: "level" (also "zero"/"home"), "min", "max", or a
        side name - "battery" / "non_battery" - which config maps onto min/max.

        min/max are this servo's OWN recorded tick at that end of the mechanism, so a
        mirrored servo automatically gets the opposite end of its range - which is what
        makes two facing servos tilt the bed the same way. They sit `margin` ticks inside
        the recorded end (default config.SERVO_SETPOINT_MARGIN), which is further in than
        the limits `apply_limits` writes to the servo."""
        name = name.lower()
        name = config.SIDE_SETPOINTS.get(name, name)   # "battery" / "non_battery" -> "max" / "min"
        if name in ("level", "zero", "home"):
            return self.zero
        if name not in ("min", "max"):
            raise ValueError(f"unknown setpoint {name!r} - use level, min, max, "
                             f"{config.BATTERY_SIDE} or {config.NON_BATTERY_SIDE}")
        if self.lower is None or self.upper is None:
            raise ValueError("end stops not measured yet - record min and max in tests/read_positions.py and save")
        margin = setpoint_margin() if margin is None else max(margin, limit_margin())
        lower, upper = self.bounds(margin)
        ticks_rise_toward_max = not self.invert
        if name == "max":
            return upper if ticks_rise_toward_max else lower
        return lower if ticks_rise_toward_max else upper

    def to_rel(self, absolute: int) -> int:
        return self.sign * (int(absolute) - self.zero)

    def rel_range(self, margin: int = 0) -> tuple[int, int]:
        """How far the mechanism coordinate can go each way for this servo."""
        ends = sorted(self.to_rel(b) for b in self.bounds(margin))
        return ends[0], ends[1]


class Calibration:
    def __init__(self, servos: dict[int, ServoCal] | None = None, path: Path | None = None) -> None:
        self.servos: dict[int, ServoCal] = dict(servos or {})
        self.path = Path(path) if path else Path(config.SERVO_CALIBRATION_FILE)

    # --- file ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Calibration":
        """The stored calibration, or an empty one if the file doesn't exist yet."""
        cal = cls(path=path)
        if cal.path.exists():
            data = json.loads(cal.path.read_text())
            cal.servos = {int(sid): ServoCal(**entry) for sid, entry in data.get("servos", {}).items()}
        return cal

    def save(self) -> Path:
        data = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "note": "absolute servo ticks (0-4095). zero = mechanism level. Written by tests/read_positions.py",
            "servos": {str(sid): asdict(entry) for sid, entry in sorted(self.servos.items())},
        }
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        return self.path

    # --- access ------------------------------------------------------------------
    def __contains__(self, servo_id: int) -> bool:
        return servo_id in self.servos

    def __getitem__(self, servo_id: int) -> ServoCal:
        if servo_id not in self.servos:
            raise KeyError(f"servo {servo_id} is not calibrated - run `python tests/read_positions.py` "
                           f"(zero / min / max / save). File: {self.path}")
        return self.servos[servo_id]

    def has_all(self, ids) -> bool:
        return all(sid in self.servos for sid in ids)

    # --- building it from measurements ---------------------------------------------
    @classmethod
    def from_measurements(cls, zero: dict[int, int], end_a: dict[int, int] | None = None,
                          end_b: dict[int, int] | None = None, path: Path | None = None) -> "Calibration":
        """zero / end_a ("min") / end_b ("max") are {servo_id: absolute ticks} taken with the
        mechanism at level and at its two end stops. A servo whose ticks go DOWN toward end_b
        is mirrored."""
        cal = cls(path=path)
        for sid, z in zero.items():
            entry = ServoCal(zero=z)
            if end_a and end_b and sid in end_a and sid in end_b:
                entry.lower, entry.upper = sorted((end_a[sid], end_b[sid]))
                entry.invert = end_b[sid] < end_a[sid]
            cal.servos[sid] = entry
        return cal

    def problems(self) -> list[str]:
        """Anything that makes this calibration unsafe to use."""
        found = []
        for sid, c in sorted(self.servos.items()):
            if c.lower is None or c.upper is None:
                continue
            if not c.lower <= c.zero <= c.upper:
                found.append(f"servo {sid}: zero {c.zero} is not between its end stops [{c.lower}, {c.upper}] "
                             "(travel probably crosses the 4095 <-> 0 boundary)")
            if c.upper - c.lower < 2 * setpoint_margin() + 10:
                found.append(f"servo {sid}: only {c.upper - c.lower} ticks between the end stops")
        return found

    def describe(self) -> str:
        if not self.servos:
            return f"No calibration stored yet ({self.path})."
        lines = [f"Calibration from {self.path}:"]
        for sid, c in sorted(self.servos.items()):
            if c.lower is None:
                lines.append(f"  servo {sid}: zero {c.zero}, end stops not measured, {'MIRRORED' if c.invert else 'normal'}")
            else:
                lo, hi = c.rel_range()
                lines.append(f"  servo {sid}: zero {c.zero}, end stops [{c.lower}, {c.upper}] = {lo:+d} .. {hi:+d} from level, "
                             f"{'MIRRORED' if c.invert else 'normal'}")
        lines.append(f"  margins: servo limits {limit_margin()} ticks inside the recorded ends, setpoints {setpoint_margin()} ticks inside")
        for sid, c in sorted(self.servos.items()):
            if c.lower is not None:
                lines.append(f"  servo {sid}: go_min -> {c.setpoint('min')}, go_level -> {c.zero}, go_max -> {c.setpoint('max')}, "
                             f"servo limits {list(c.bounds(limit_margin()))}")
        return "\n".join(lines + [f"  !! {p}" for p in self.problems()])

    # --- pushing the end stops into the servos ------------------------------------------
    def apply_limits(self, bus, margin: int | None = None) -> dict[int, tuple[int, int]]:
        """Write each servo's travel (minus a margin) into its EEPROM angle limits, so the servo
        itself refuses to go past them whatever the code asks for. Returns what was written."""
        if self.problems():
            raise ValueError("refusing to apply limits: " + "; ".join(self.problems()))
        margin = limit_margin() if margin is None else margin
        written = {}
        for sid, c in sorted(self.servos.items()):
            if c.lower is None or c.upper is None:
                continue
            limits = c.bounds(margin)
            bus.set_angle_limits(sid, *limits)
            written[sid] = limits
        return written


def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m actuators.calibration", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["show", "apply"])
    ap.add_argument("--margin", type=int, default=limit_margin(),
                    help=f"ticks inside each recorded end (default {limit_margin()}, from config.SERVO_LIMIT_MARGIN)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    cal = Calibration.load()
    print(cal.describe())
    if args.cmd == "show" or not cal.servos:
        return 0

    from actuators.sts_bus import StsBus, StsBusError

    plan = {sid: c.bounds(args.margin) for sid, c in sorted(cal.servos.items()) if c.lower is not None}
    print("\nWill write these angle limits to the servos' EEPROM:")
    for sid, limits in plan.items():
        print(f"  servo {sid}: {list(limits)}")
    if not args.yes:
        input("Press Enter to write them, Ctrl+C to abort... ")
    try:
        bus = StsBus(config.SERVO_PORT, config.SERVO_BAUDRATE)
    except OSError as e:
        print(f"Could not open {config.SERVO_PORT}: {e}")
        return 1
    try:
        for sid in plan:
            position = bus.feedback(sid)["position"]
            if not plan[sid][0] <= position <= plan[sid][1]:
                print(f"servo {sid} is at {position}, OUTSIDE the limits about to be written. Move it inside its travel first.")
                return 2
        written = cal.apply_limits(bus, args.margin)
        for sid in written:
            print(f"  servo {sid}: limits now {bus.angle_limits(sid)}")
        return 0
    except (StsBusError, ValueError) as e:
        print(f"{type(e).__name__}: {e}")
        return 1
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
