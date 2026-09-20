"""PHYSICAL tool: is it the Pi's GPIO pin, the jumper wires, or the HX711 board?

Three checks, from "just the Pi" outward. Not a pytest test. Run from tests/ (or `python -m tests.gpio_check`):

  1) python gpio_check.py pins                 # what is each pin seeing right now?
     python gpio_check.py pins 17 27 5 6       #   (defaults to the HX711 pins in config)
        Reads each pin with the Pi's internal pull-UP, then pull-DOWN:
          follows both  -> FLOATING: nothing is driving it (wire off, or the far end is an input / unpowered)
          always 0      -> HELD LOW by whatever is attached
          always 1      -> HELD HIGH by whatever is attached
        A pin that follows the pulls is electrically alive as an INPUT.

  2) python gpio_check.py loopback 17 27       # are these two Pi pins themselves OK?
        UNPLUG the HX711 first, then join the two pins with ONE jumper wire.
        Drives A and reads B, then drives B and reads A, high and low, 50 times each.
        PASS = both pins can output and input -> the Pi side is fine; the fault is the wires or the board.
        FAIL = a dead pin (or the jumper isn't making contact - try it on two other pins to tell which).

  3) python gpio_check.py hx711                # is the chip alive on the configured pins?
     python gpio_check.py hx711 --dout 17 --sck 27
        Step by step: DOUT level at rest -> wake the chip -> does DOUT pulse LOW ~10x a second
        (that is the chip saying "sample ready", and it needs only VCC, GND and the DT wire) ->
        clock out samples (that needs the SCK wire too). The step that fails names the culprit.

Header pin numbers for BCM GPIOs:  17=pin 11, 27=pin 13, 22=pin 15, 23=pin 16, 5=pin 29, 6=pin 31, 13=pin 33.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

PHYSICAL = {2: 3, 3: 5, 4: 7, 14: 8, 15: 10, 17: 11, 18: 12, 27: 13, 22: 15, 23: 16, 24: 18, 10: 19, 9: 21, 25: 22,
            11: 23, 8: 24, 7: 26, 0: 27, 1: 28, 5: 29, 6: 31, 12: 32, 13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40}


def name(gpio: int) -> str:
    return f"GPIO{gpio} (pin {PHYSICAL.get(gpio, '?')})"


def open_chip():
    import lgpio

    from sensors.hx711 import LgpioPins

    # reuse the driver's chip discovery without claiming any pins
    finder = LgpioPins.__new__(LgpioPins)
    finder._lgpio = lgpio
    return lgpio, finder._open_header_chip()


def sample(lgpio, handle, gpio: int, flags: int, count: int = 20) -> list[int]:
    lgpio.gpio_claim_input(handle, gpio, flags)
    time.sleep(0.05)
    values = []
    for _ in range(count):
        values.append(lgpio.gpio_read(handle, gpio))
        time.sleep(0.005)
    lgpio.gpio_free(handle, gpio)
    return values


def classify(up: list[int], down: list[int]) -> str:
    if all(up) and not any(down):
        return "FLOATING - nothing is driving this pin (pin itself is alive as an input)"
    if not any(up) and not any(down):
        return "HELD LOW by whatever is attached"
    if all(up) and all(down):
        return "HELD HIGH by whatever is attached"
    return f"CHANGING (pull-up {sum(up)}/{len(up)} high, pull-down {sum(down)}/{len(down)} high) - something is toggling it"


def cmd_pins(gpios: list[int]) -> int:
    lgpio, handle = open_chip()
    try:
        for gpio in gpios:
            try:
                up = sample(lgpio, handle, gpio, lgpio.SET_PULL_UP)
                down = sample(lgpio, handle, gpio, lgpio.SET_PULL_DOWN)
                print(f"{name(gpio):<18} {classify(up, down)}")
            except lgpio.error as e:
                print(f"{name(gpio):<18} could not be claimed: {e} (another program has it open, or the kernel reserves it)")
    finally:
        lgpio.gpiochip_close(handle)
    return 0


def cmd_loopback(a: int, b: int, yes: bool) -> int:
    print(f"Loopback {name(a)} <-> {name(b)}.")
    print("UNPLUG the HX711 (and anything else) from both pins, then join the two pins with ONE jumper wire.")
    if not yes:
        try:
            input("Done? Press Enter to test, Ctrl+C to abort... ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 130
    lgpio, handle = open_chip()
    failures = []
    try:
        for out_pin, in_pin in ((a, b), (b, a)):
            lgpio.gpio_claim_output(handle, out_pin, 0)
            lgpio.gpio_claim_input(handle, in_pin, lgpio.SET_PULL_DOWN)
            wrong = {0: 0, 1: 0}
            for i in range(100):
                level = i % 2
                # fight the driven level with the opposite pull so an unconnected pin can't pass by luck
                lgpio.gpio_free(handle, in_pin)
                lgpio.gpio_claim_input(handle, in_pin, lgpio.SET_PULL_DOWN if level else lgpio.SET_PULL_UP)
                lgpio.gpio_write(handle, out_pin, level)
                time.sleep(0.002)
                if lgpio.gpio_read(handle, in_pin) != level:
                    wrong[level] += 1
            lgpio.gpio_write(handle, out_pin, 0)
            lgpio.gpio_free(handle, out_pin)
            lgpio.gpio_free(handle, in_pin)
            ok = not wrong[0] and not wrong[1]
            print(f"  drive {name(out_pin)} -> read {name(in_pin)}: {'PASS' if ok else 'FAIL'}"
                  + ("" if ok else f"  (wrong when driving low: {wrong[0]}/50, when driving high: {wrong[1]}/50)"))
            if not ok:
                failures.append((out_pin, in_pin, wrong))
    finally:
        lgpio.gpiochip_close(handle)

    if not failures:
        print(f"\nPASS - both {name(a)} and {name(b)} work as output AND input. The Pi side is fine:\n"
              "the fault is in the jumper wires to the HX711, the HX711's power, or the HX711 board itself.")
        return 0
    if len(failures) == 2 and all(w[0] == 0 and w[1] == 50 for _, _, w in failures):
        print("\nFAIL both ways, and only when driving HIGH: the two pins are not connected to each other.\n"
              "Most likely the jumper isn't making contact (or is on the wrong holes) - reseat it, or try a different jumper.")
    else:
        print("\nFAIL - to tell a dead pin from a bad jumper, repeat with one of these pins and a known-free pin\n"
              "(e.g. `loopback 22 23`, pins 15 and 16). The pin that fails in every pairing is the dead one.")
    return 1


def cmd_hx711(dout: int, sck: int) -> int:
    print(f"HX711 check: DT/DOUT on {name(dout)}, SCK on {name(sck)}")
    lgpio, handle = open_chip()
    try:
        up = sample(lgpio, handle, dout, lgpio.SET_PULL_UP)
        down = sample(lgpio, handle, dout, lgpio.SET_PULL_DOWN)
        print(f"1. DOUT at rest: {classify(up, down)}")
        floating = all(up) and not any(down)

        lgpio.gpio_claim_input(handle, dout, lgpio.SET_PULL_UP)
        lgpio.gpio_claim_output(handle, sck, 0)
        lgpio.gpio_write(handle, sck, 1)          # > 60 us high = power down ...
        time.sleep(0.002)
        lgpio.gpio_write(handle, sck, 0)          # ... low = wake up
        print("2. sent a reset pulse on SCK, watching DOUT for 2 s...")
        edges, last, lows, total = 0, 1, 0, 0
        end = time.monotonic() + 2.0
        first_low = None
        start = time.monotonic()
        while time.monotonic() < end:
            level = lgpio.gpio_read(handle, dout)
            total += 1
            lows += level == 0
            if last == 1 and level == 0:
                edges += 1
                if first_low is None:
                    first_low = time.monotonic() - start
            last = level
            time.sleep(0.0005)
        print(f"   DOUT was low in {lows}/{total} samples, went low {edges} time(s)"
              + (f", first after {first_low * 1000:.0f} ms" if first_low is not None else ""))
    finally:
        lgpio.gpiochip_close(handle)

    if lows == 0:
        print("\nVERDICT: the chip never signalled 'sample ready'. This step needs only VCC, GND and the DT wire - SCK is not involved yet.")
        if floating:
            print("  DOUT is FLOATING, so the Pi pin is fine but nothing is driving it:\n"
                  "   - the DT wire is not connected at one end (or is on the wrong header pin), or\n"
                  "   - the HX711 has no power: measure VCC-to-GND ON THE HX711 BOARD, it must read 3.3 V.")
        else:
            print("  DOUT is being HELD HIGH. Either the chip is stuck in power-down because its SCK input is high\n"
                  "  (SCK wire on a 3.3 V pin / swapped with VCC?), or something else is on this pin pulling it up\n"
                  "  (the metal sensor is also configured on GPIO17!).")
        print("  To rule the Pi pin out completely: `python gpio_check.py loopback <dout> <sck>` with the HX711 unplugged.")
        return 1
    if lows == total:
        print("\nVERDICT: DOUT is low ALL the time and never pulses. A live chip raises it between samples.\n"
              "  Unpowered board (its pins clamp low), DT wired to GND, or DT/SCK swapped.")
        return 1

    print("   -> the chip is ALIVE and converting (VCC, GND and DT are good). Now clocking samples out - this tests the SCK wire:")
    from sensors.hx711 import HX711, HX711Error

    try:
        adc = HX711(dout, sck)
        values = [adc.read_raw() for _ in range(20)]
        adc.close()
    except HX711Error as e:
        print(f"3. reading FAILED: {type(e).__name__}: {e}")
        print("\nVERDICT: the chip is alive but can't be clocked -> the SCK wire (or that Pi pin). "
              "Run the loopback test on these two pins to check the Pi side.")
        return 1
    spread = statistics.pstdev(values)
    print(f"3. 20 samples: median {int(statistics.median(values))}, stdev {spread:.0f} counts")
    if spread == 0:
        print("\nVERDICT: values don't jitter at all - not a real conversion. Check the load cell's four wires (E+ E- A- A+).")
        return 1
    print("\nVERDICT: WORKING. Pins, wires and board are all fine. Next: python load_cell_read.py")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python gpio_check.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pins")
    p.add_argument("gpios", type=int, nargs="*", default=[config.HX711_DOUT_PIN, config.HX711_SCK_PIN])
    p = sub.add_parser("loopback")
    p.add_argument("a", type=int)
    p.add_argument("b", type=int)
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("hx711")
    p.add_argument("--dout", type=int, default=config.HX711_DOUT_PIN)
    p.add_argument("--sck", type=int, default=config.HX711_SCK_PIN)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "pins":
            return cmd_pins(args.gpios)
        if args.cmd == "loopback":
            return cmd_loopback(args.a, args.b, args.yes)
        return cmd_hx711(args.dout, args.sck)
    except Exception as e:  # noqa: BLE001 - lgpio errors are not a stable hierarchy
        if "busy" in str(e).lower():
            print(f"A pin is already in use by another program ({e}). Is load_cell_read.py or main.py still running?")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
