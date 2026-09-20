"""PHYSICAL tool: read the HX711 load cell live, tare it, calibrate it, save the result.

Wiring (from config): DOUT/DT -> GPIO5 (pin 29), SCK -> GPIO6 (pin 31), VCC -> 3.3 V, GND -> GND.
Not a pytest test (no test_ prefix). Run it by hand:

    python load_cell_read.py            # from tests/
    python -m tests.load_cell_read      # same thing, from sorter/

While it runs you see raw counts, grams and the noise over the last second. Type + Enter:

    t              tare: the scale is EMPTY right now, call this zero
    c 200          calibrate: a known 200 g weight is on the (tared) scale
    save           store zero + scale in the calibration file the whole project loads
    q              finish   (Ctrl+C works too)

Calibrating, start to finish:  empty the scale -> t -> put a known weight on -> c <grams>
-> check it reads right -> take it off, check it returns to ~0 -> save.
No reference weight? A full 355 ml drink can is ~385 g, a phone is on its spec sheet,
a kitchen scale will weigh anything for you.
"""
from __future__ import annotations

import argparse
import select
import statistics
import sys
from collections import deque
from pathlib import Path

# So this runs both as `python -m tests.load_cell_read` from sorter/ and as `python load_cell_read.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from sensors.hx711 import HX711Error
from sensors.load_cell import LoadCell


def _read_line() -> str | None:
    """A typed line if one is waiting, else None straight away."""
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return None
    line = sys.stdin.readline()
    return "q" if line == "" else line.strip()  # "" = stdin closed


def handle(cell: LoadCell, line: str) -> str | None:
    """Carry out one typed command. Returns a message, or None to quit."""
    parts = line.lower().split()
    if not parts:
        return ""
    command = parts[0]
    if command in ("q", "quit", "exit"):
        return None
    if command in ("t", "tare"):
        return f"tared: zero = {cell.tare()}"
    if command in ("c", "cal", "calibrate"):
        try:
            grams = float(parts[1])
            unit = cell.calibrate(grams)
        except IndexError:
            return "say how heavy it is, e.g.:  c 200"
        except ValueError as e:
            return f"not calibrated: {e}"
        swapped = "  (negative = green/white swapped; fine, software handles it)" if unit < 0 else ""
        return f"calibrated with {grams:g} g: {unit:.3f} counts per gram{swapped}"
    if command == "save":
        return f"saved -> {cell.save()}  (zero {cell.zero_offset}, {cell.reference_unit:.3f} counts/g)"
    return f"unknown command {line!r} - use: t | c <grams> | save | q"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python load_cell_read.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=0, help="stop after this many readings (default 0 = until q)")
    args = ap.parse_args(argv)

    try:
        cell = LoadCell(mock=False)
    except Exception as e:  # noqa: BLE001 - whatever stops the pins opening, say it plainly
        print(f"Could not open the HX711 on DOUT=GPIO{config.HX711_DOUT_PIN} / SCK=GPIO{config.HX711_SCK_PIN}: {e}")
        return 1

    uncalibrated = cell.reference_unit == 1.0
    print(f"HX711 on DOUT=GPIO{config.HX711_DOUT_PIN}, SCK=GPIO{config.HX711_SCK_PIN}. "
          f"zero {cell.zero_offset}, {cell.reference_unit:.3f} counts/g"
          + ("  <- NOT calibrated yet: grams below are just raw counts" if uncalibrated else ""))
    print("t = tare (scale empty) | c <grams> = calibrate with a known weight | save | q = finish\n")

    recent: deque[int] = deque(maxlen=10)  # ~1 s at 10 samples/s
    count = 0
    try:
        while not args.samples or count < args.samples:
            raw = cell.read_raw()
            count += 1
            recent.append(raw)
            grams = (raw - cell.zero_offset) / cell.reference_unit
            noise = statistics.pstdev(recent) / abs(cell.reference_unit) if len(recent) > 2 else 0.0
            print(f"\r  raw {raw:>9}   {grams:>9.2f} g   noise ±{noise:.2f} g    > ", end="", flush=True)
            line = _read_line()
            if line is None:
                continue
            message = handle(cell, line)
            if message is None:
                break
            if message:
                print(f"  {message}")
                recent.clear()
    except KeyboardInterrupt:
        pass
    except HX711Error as e:
        print(f"\n{type(e).__name__}: {e}")
        return 1
    finally:
        cell.close()
    print(f"\nzero {cell.zero_offset}, {cell.reference_unit:.3f} counts/g"
          + ("" if config.LOAD_CELL_CALIBRATION_FILE.exists() else "  (nothing saved)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
