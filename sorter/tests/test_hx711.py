"""sensors.hx711 and sensors.load_cell against a simulated HX711 - no hardware.

Run with:
    cd sorter
    python -m pytest tests/test_hx711.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from sensors import hx711 as hx711_module
from sensors.hx711 import HX711, HX711Error, HX711NotReady, HX711Saturated, HX711StuckLow
from sensors.load_cell import LoadCell, load_calibration
from tests import load_cell_read


class FakeChip:
    """The HX711 as seen from its two pins.

    DOUT low = a conversion is ready. Each SCK rising edge shifts out the next bit, MSB first;
    after bit 24 DOUT goes high, and the 1-3 extra pulses pick the gain for the next conversion.
    SCK held high across a sleep = power-down/reset, exactly as the real part treats > 60 us."""

    def __init__(self, values, ready=True):
        self.values = list(values)       # signed values to serve in order; the last one repeats
        self.ready = ready
        self.index = 0
        self.pulses = 0                  # rising edges since this word started
        self.sck = 0
        self.pulse_counts = []           # pulses used by each completed read
        self.busy_polls = 0              # DOUT stays HIGH for a while after a word, like the real chip
        self.resets = 0
        self.closed = False

    def _word(self):
        return self.values[min(self.index, len(self.values) - 1)] & 0xFFFFFF

    def read_dout(self):
        if self.pulses >= 25:            # word finished: converting again, DOUT high until it is done
            self.pulse_counts.append(self.pulses)
            self.index += 1
            self.pulses = 0
            self.busy_polls = 1
        if self.busy_polls:
            self.busy_polls -= 1
            return 1
        if self.pulses == 0:
            return 0 if self.ready else 1
        return (self._word() >> (24 - self.pulses)) & 1

    def write_sck(self, level):
        if level == 1 and self.sck == 0:
            self.pulses += 1
        self.sck = level

    def slept(self):
        if self.sck == 1:                # held high "for a long time": power-down, word abandoned
            self.pulses = 0
            self.resets += 1

    def close(self):
        self.closed = True


@pytest.fixture
def chip_factory(monkeypatch):
    chips = []
    monkeypatch.setattr(hx711_module.time, "sleep", lambda s: [chip.slept() for chip in chips])

    def make(values, **kwargs):
        chips.append(FakeChip(values, **kwargs))
        return chips[-1]
    return make


def test_reads_positive_and_negative_values(chip_factory):
    chip = chip_factory([0, 123456, -294284, 1, -1])      # first word is eaten by reset()
    adc = HX711(5, 6, pins=chip)
    assert [adc.read_raw() for _ in range(4)] == [123456, -294284, 1, -1]


def test_gain_selects_the_number_of_clock_pulses(chip_factory):
    for gain, pulses in ((128, 25), (32, 26), (64, 27)):
        chip = chip_factory([0, 1000, 1000])
        adc = HX711(5, 6, gain=gain, pins=chip)
        adc.read_raw()
        adc.is_ready()                                      # a poll closes the last word
        assert chip.pulse_counts == [pulses, pulses]        # the reset read + ours
        assert chip.resets == 1
    with pytest.raises(ValueError):
        HX711(5, 6, gain=99, pins=chip_factory([0]))


def test_not_ready_is_reported_with_a_wiring_hint(chip_factory, monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(hx711_module.time, "monotonic", lambda: t.__setitem__("now", t["now"] + 0.3) or t["now"])
    adc = HX711(5, 6, pins=chip_factory([0], ready=False))  # reset() swallows the first timeout quietly
    with pytest.raises(HX711NotReady, match="DOUT stayed high"):
        adc.read_raw(timeout_s=1.0)


def test_a_line_stuck_low_is_an_error_not_zero_grams():
    class DeadLine:                                  # unpowered chip / DOUT and SCK swapped / wire off
        def read_dout(self): return 0
        def write_sck(self, level): pass
        def close(self): pass

    adc = HX711(5, 6, pins=DeadLine())
    with pytest.raises(HX711StuckLow, match="stuck LOW"):
        adc.read_raw()


def test_a_genuine_zero_reading_is_still_allowed(chip_factory):
    adc = HX711(5, 6, pins=chip_factory([0, 0, 0]))   # real chip: DOUT goes high after the word
    assert adc.read_raw() == 0


def test_saturated_reading_is_an_error_not_a_weight(chip_factory):
    adc = HX711(5, 6, pins=chip_factory([0, 0x7FFFFF]))
    with pytest.raises(HX711Saturated, match="bridge wire"):
        adc.read_raw()
    adc = HX711(5, 6, pins=chip_factory([0, -0x800000]))
    with pytest.raises(HX711Saturated):
        adc.read_raw()


def test_a_read_with_a_slow_clock_pulse_is_discarded_and_retried(chip_factory, monkeypatch):
    chip = chip_factory([0, 111, 222])
    adc = HX711(5, 6, pins=chip)
    real = adc._read_once
    calls = {"n": 0}

    def once(timeout_s):
        value, _slowest = real(timeout_s)
        calls["n"] += 1
        return value, (80.0 if calls["n"] == 1 else 4.0)   # first read: a pulse ran 80 us (> 60 us limit)

    monkeypatch.setattr(adc, "_read_once", once)
    assert adc.read_raw() == 222                            # 111 was thrown away
    assert adc.bad_reads == 1


def test_gives_up_if_every_read_is_disturbed(chip_factory, monkeypatch):
    adc = HX711(5, 6, pins=chip_factory([0, 5]))
    monkeypatch.setattr(adc, "_read_once", lambda timeout_s: (5, 500.0))
    with pytest.raises(HX711Error, match="disturbed"):
        adc.read_raw()


def test_median_ignores_one_wild_sample(chip_factory):
    adc = HX711(5, 6, pins=chip_factory([0, 1000, 1002, 900000, 998, 1001]))
    assert adc.read_median(5) == 1001


# --- LoadCell on top of it -----------------------------------------------------------------
@pytest.fixture
def cal_file(tmp_path, monkeypatch):
    path = tmp_path / "load_cell_calibration.json"
    monkeypatch.setattr(config, "LOAD_CELL_CALIBRATION_FILE", path)
    return path


def make_cell(chip_factory, values):
    return LoadCell(mock=False, hx711=HX711(5, 6, pins=chip_factory([0, *values])))


def test_tare_calibrate_weigh_save_reload(chip_factory, cal_file):
    empty, per_gram = -294300, 1074.0
    with_200g = int(empty + 200 * per_gram)
    with_48g = int(empty + 48.2 * per_gram)
    cell = make_cell(chip_factory, [empty] * 15 + [with_200g] * 15 + [with_48g])

    assert cell.tare() == empty
    assert cell.calibrate(200) == pytest.approx(per_gram, rel=1e-3)
    assert cell.read_weight_g() == pytest.approx(48.2, abs=0.05)

    assert cell.save() == cal_file
    assert load_calibration() == {"zero_offset": empty, "reference_unit": pytest.approx(per_gram, rel=1e-3)}
    reloaded = make_cell(chip_factory, [with_48g])          # a fresh process picks the file up
    assert reloaded.read_weight_g() == pytest.approx(48.2, abs=0.05)


def test_swapped_signal_wires_still_give_positive_grams(chip_factory, cal_file):
    empty = 50000
    cell = make_cell(chip_factory, [empty] * 15 + [empty - 100000] * 15 + [empty - 50000])
    cell.tare()
    assert cell.calibrate(100) < 0                          # counts go DOWN with load: A+/A- swapped
    assert cell.read_weight_g() == pytest.approx(50.0)


def test_calibrate_refuses_nonsense(chip_factory, cal_file):
    cell = make_cell(chip_factory, [1000] * 40)
    cell.tare()
    with pytest.raises(ValueError, match="did not change"):
        cell.calibrate(200)                                 # nothing was put on
    with pytest.raises(ValueError):
        cell.calibrate(0)


def test_without_a_file_the_config_placeholders_apply(cal_file):
    assert load_calibration() == {"zero_offset": config.HX711_ZERO_OFFSET, "reference_unit": config.HX711_REFERENCE_UNIT}


def test_mock_mode_needs_no_pins(cal_file):
    cell = LoadCell(mock=True)
    assert abs(cell.read_weight_g() - config.MOCK_WEIGHT_BASELINE_G) <= config.MOCK_WEIGHT_JITTER_G + 1
    assert cell.stable_reading() == pytest.approx(config.MOCK_WEIGHT_BASELINE_G, abs=2)
    cell.close()


# --- the interactive tool's commands ----------------------------------------------------------
def test_tool_commands(chip_factory, cal_file):
    empty, per_gram = -294300, 1074.0
    cell = make_cell(chip_factory, [empty] * 15 + [int(empty + 385 * per_gram)] * 15)
    assert "tared: zero = -294300" in load_cell_read.handle(cell, "t")
    assert "say how heavy" in load_cell_read.handle(cell, "c")
    assert "1074" in load_cell_read.handle(cell, "c 385")
    assert "saved ->" in load_cell_read.handle(cell, "save") and cal_file.exists()
    assert "unknown command" in load_cell_read.handle(cell, "weigh")
    assert load_cell_read.handle(cell, "q") is None
