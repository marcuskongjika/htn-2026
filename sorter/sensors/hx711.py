"""HX711 24-bit load-cell amplifier, bit-banged from two GPIO pins.

    DOUT (a.k.a. DT) -> GPIO input     the chip pulls it LOW when a conversion is ready
    SCK              -> GPIO output    we clock the 24 bits out, MSB first

One read = wait for DOUT low, 24 clock pulses (read DOUT after each rising
edge), then 1-3 extra pulses that pick the channel/gain for the NEXT read:

    25 pulses -> channel A, gain 128   (the normal load-cell setting)
    26 pulses -> channel B, gain 32
    27 pulses -> channel A, gain 64

The one timing rule that matters: if SCK stays HIGH for more than 60 us the
HX711 takes it as "power down" and the conversion is lost. Python on Linux can
be pre-empted mid-pulse, so every pulse is timed and a read whose slowest pulse
ran long is thrown away and retried instead of trusted. This talks to lgpio
directly (about 4 us a pulse on a Pi 5; gpiozero is 3x slower with 40 us
outliers). Measured on the rig: 10 samples/s, noise ~66 counts on a 2 kg cell.

The value is 24-bit two's complement. 0x7FFFFF / 0x800000 mean the input is
saturated (overload, or a load-cell wire off) and are reported as errors.
"""
from __future__ import annotations

import logging
import statistics
import time

log = logging.getLogger(__name__)

GAIN_PULSES = {128: 25, 32: 26, 64: 27}
MAX_SCK_HIGH_US = 50.0          # the chip's limit is 60 us; stay clear of it
SATURATED = (0x7FFFFF, -0x800000)


class HX711Error(RuntimeError):
    """Base class for everything this module raises."""


class HX711NotReady(HX711Error):
    """DOUT never went low: chip unpowered, DOUT/SCK wired wrong, or SCK stuck high."""


class HX711Saturated(HX711Error):
    """The ADC is pinned at full scale: overloaded cell, or a bridge wire (E+/E-/A+/A-) is off."""


class LgpioPins:
    """The two pins, through lgpio. Finds the header's gpiochip itself (Pi 5: the RP1 chip,
    usually gpiochip4; older Pis: gpiochip0)."""

    def __init__(self, dout: int, sck: int) -> None:
        import lgpio  # only needed on the Pi

        self._lgpio = lgpio
        self._handle = self._open_header_chip()
        self.dout, self.sck = dout, sck
        try:
            lgpio.gpio_claim_input(self._handle, dout)
            lgpio.gpio_claim_output(self._handle, sck, 0)
        except lgpio.error as e:
            lgpio.gpiochip_close(self._handle)
            self._handle = None
            if "busy" in str(e).lower():
                raise HX711Error(f"GPIO{dout}/GPIO{sck} are already in use - another program has the HX711 open "
                                 "(is load_cell_read.py or main.py still running in another terminal?)") from e
            raise HX711Error(f"could not claim GPIO{dout}/GPIO{sck}: {e}") from e

    def _open_header_chip(self) -> int:
        lgpio = self._lgpio
        fallback = None
        for number in range(8):
            try:
                handle = lgpio.gpiochip_open(number)
            except lgpio.error:
                continue
            _ok, _lines, _name, label = lgpio.gpio_get_chip_info(handle)
            if "rp1" in label.lower():           # Pi 5 header
                if fallback is not None:
                    lgpio.gpiochip_close(fallback)
                return handle
            if fallback is None and "bcm" in label.lower():
                fallback = handle                 # Pi 4 and earlier
            else:
                lgpio.gpiochip_close(handle)
        if fallback is None:
            raise HX711Error("no usable gpiochip found (is this a Raspberry Pi, and is the user in the gpio/dialout group?)")
        return fallback

    def read_dout(self) -> int:
        return self._lgpio.gpio_read(self._handle, self.dout)

    def write_sck(self, level: int) -> None:
        self._lgpio.gpio_write(self._handle, self.sck, level)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._lgpio.gpio_write(self._handle, self.sck, 0)
            finally:
                self._lgpio.gpiochip_close(self._handle)
                self._handle = None


class HX711:
    def __init__(self, dout: int, sck: int, gain: int = 128, pins=None) -> None:
        """pins: anything with read_dout() / write_sck(level) / close() - the tests pass a fake."""
        if gain not in GAIN_PULSES:
            raise ValueError(f"gain must be one of {sorted(GAIN_PULSES)}")
        self.gain = gain
        self._pins = pins if pins is not None else LgpioPins(dout, sck)
        self.bad_reads = 0                       # reads discarded for timing; worth logging if it climbs
        self.reset()

    # --- chip control ------------------------------------------------------------------
    def reset(self) -> None:
        """Power-cycle the chip through SCK (high > 60 us = power down, low = wake up), then do
        one read so the gain setting applies from the next sample on."""
        self._pins.write_sck(1)
        time.sleep(0.001)
        self._pins.write_sck(0)
        time.sleep(0.05)
        try:
            self._read_once(timeout_s=1.0)
        except HX711Error:
            pass                                 # the real read that follows will report it properly

    def power_down(self) -> None:
        self._pins.write_sck(1)
        time.sleep(0.001)

    def is_ready(self) -> bool:
        return self._pins.read_dout() == 0

    # --- reading ---------------------------------------------------------------------------
    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while self._pins.read_dout():
            if time.monotonic() > deadline:
                raise HX711NotReady(
                    f"HX711 never signalled ready within {timeout_s:g} s (DOUT stayed high) - "
                    "check VCC/GND, that DOUT and SCK are not swapped, and the load-cell wiring"
                )
            time.sleep(0.0005)

    def _read_once(self, timeout_s: float) -> tuple[int, float]:
        """(signed value, slowest SCK-high time in us)."""
        self._wait_ready(timeout_s)
        pins, clock = self._pins, time.perf_counter_ns
        value, slowest = 0, 0
        for _ in range(24):
            start = clock()
            pins.write_sck(1)
            bit = pins.read_dout()
            pins.write_sck(0)
            slowest = max(slowest, clock() - start)
            value = (value << 1) | bit
        for _ in range(GAIN_PULSES[self.gain] - 24):
            start = clock()
            pins.write_sck(1)
            pins.write_sck(0)
            slowest = max(slowest, clock() - start)
        if value & 0x800000:
            value -= 1 << 24
        return value, slowest / 1000.0

    def read_raw(self, timeout_s: float = 1.0, retries: int = 5) -> int:
        """One trustworthy sample. A read during which any clock pulse ran too long is dropped
        and taken again - that is the cure for the occasional wild value HX711s are known for."""
        for _ in range(retries):
            value, slowest_us = self._read_once(timeout_s)
            if slowest_us > MAX_SCK_HIGH_US:
                self.bad_reads += 1
                log.debug("discarding HX711 read: a clock pulse took %.0f us", slowest_us)
                continue
            if value in SATURATED:
                raise HX711Saturated(
                    f"HX711 reads full scale ({value}) - the cell is overloaded or a bridge wire "
                    "(red E+, black E-, white A-, green A+) is loose"
                )
            return value
        raise HX711Error(f"{retries} reads in a row were disturbed mid-clock - is the Pi heavily loaded?")

    def read_median(self, samples: int = 5, timeout_s: float = 1.0) -> int:
        """Median of several samples: shrugs off a single glitch without smearing a real change."""
        return int(statistics.median(self.read_raw(timeout_s) for _ in range(samples)))

    def close(self) -> None:
        self._pins.close()
