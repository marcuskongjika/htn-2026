"""actuators.servo_pair against two simulated servos on one wire.

Run with:
    cd sorter
    python -m pytest tests/test_servo_pair.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators import servo_pair
from actuators.servo_pair import ServoPair
from actuators.sts_bus import StsBus, StsBusError
from tests.test_move_pair import PairWire


class ModalPairWire(PairWire):
    """PairWire whose servos only chase a goal position in position mode, and
    which records every (mode, signed speed) command seen while in wheel mode."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spins = {sid: [] for sid in self.regs}

    def _apply(self, sid, address, data):
        if address == 46 and len(data) == 2 and self.regs[sid][33] == 1:  # goal speed, in wheel mode
            raw = int.from_bytes(data, "little")
            self.spins[sid].append(-(raw & 0x7FFF) if raw & 0x8000 else raw)
        super()._apply(sid, address, data)


def make_pair(invert=(False, False), positions=None, **kwargs):
    wire = ModalPairWire(positions or {43: 2053, 13: 2840})
    bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
    return ServoPair((43, 13), invert=invert, bus=bus, mock=False, **kwargs), wire


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """run_for(2.0) should not take 2 s in the test suite: make the clock jump."""
    clock = {"t": 0.0}
    monkeypatch.setattr(servo_pair, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(servo_pair, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + max(s, 0.01)))


# --- position mode ---------------------------------------------------------------
def test_startup_holds_current_position_with_torque_limit():
    pair, wire = make_pair(torque_limit=400)
    assert wire.goals == {43: [2053], 13: [2840]}            # goal = where it already is: no jump
    assert wire.word(43, 48) == 400 and wire.word(13, 48) == 400
    assert wire.regs[43][40] == 1 and wire.regs[13][40] == 1  # holding


def test_move_by_same_direction():
    pair, wire = make_pair()
    assert pair.move_by(300) is True
    assert pair.positions() == (2353, 3140)


def test_move_by_with_follower_inverted():
    pair, wire = make_pair(invert=(False, True))
    assert pair.move_by(300) is True
    assert pair.positions() == (2353, 2540)
    pair.move_by(-300)
    assert pair.positions() == (2053, 2840)


def test_move_to_rejects_out_of_range():
    pair, _ = make_pair()
    with pytest.raises(ValueError):
        pair.move_to((2000, 5000))


def test_moves_go_out_as_one_packet():
    pair, wire = make_pair()
    before = len(wire.sync_packets)
    pair.move_by(100)
    assert len(wire.sync_packets) == before + 1


# --- wheel mode --------------------------------------------------------------------
def test_run_for_spins_then_stops_and_restores_position_mode():
    pair, wire = make_pair(invert=(False, True))
    pair.run_for(2.0, speed=400)
    assert wire.spins[43] == [400, 0]                         # start, then stop
    assert wire.spins[13] == [-400, 0]                        # mirrored follower
    assert wire.regs[43][33] == 0 and wire.regs[13][33] == 0  # back in position mode
    assert wire.regs[43][40] == 1                             # and holding


def test_speed_commands_never_move_a_servo_that_is_in_position_mode():
    # Regression: stop() used to send a block that also wrote goal position 0. Sent to a
    # servo already back in position mode (as close() does), that means "go to 0, full speed".
    pair, wire = make_pair()
    pair.run_for(1.0, speed=400)
    pair.stop()
    pair.stop()
    pair.close()
    assert (wire.word(43, 56), wire.word(13, 56)) == (2053, 2840)   # nobody went anywhere
    assert 0 not in wire.goals[43] and 0 not in wire.goals[13]


def test_run_for_negative_speed_reverses_both():
    pair, wire = make_pair(invert=(False, True))
    pair.run_for(0.5, speed=-250)
    assert wire.spins[43][0] == -250 and wire.spins[13][0] == 250


def test_run_for_stops_even_when_something_goes_wrong(monkeypatch):
    pair, wire = make_pair()
    real_feedback = pair._bus.feedback
    calls = {"n": 0}

    def flaky(sid):
        calls["n"] += 1
        if calls["n"] == 3:
            raise StsBusError("bus fell over mid-spin")
        return real_feedback(sid)

    monkeypatch.setattr(pair._bus, "feedback", flaky)
    with pytest.raises(StsBusError):
        pair.run_for(5.0, speed=400)
    assert wire.spins[43][-1] == 0 and wire.spins[13][-1] == 0  # both were told to stop
    assert wire.regs[43][33] == 0 and wire.regs[13][33] == 0


def test_run_for_stops_early_on_servo_error():
    pair, wire = make_pair()
    wire.regs[13][65] = 0b00100000                              # overload flag
    with pytest.raises(StsBusError, match="overload"):
        pair.run_for(5.0, speed=400)
    assert wire.spins[13][-1] == 0


def test_run_for_validates_arguments():
    pair, _ = make_pair()
    with pytest.raises(ValueError):
        pair.run_for(0)
    with pytest.raises(ValueError):
        pair.run_for(1.0, speed=9000)


# --- misc ------------------------------------------------------------------------------
def test_missing_servo_is_reported():
    wire = ModalPairWire({43: 2053})
    bus = StsBus("fake", serial_port=wire, echo=True, reply_timeout_s=0.01)
    with pytest.raises(RuntimeError, match=r"\[13\]"):
        ServoPair((43, 13), bus=bus, mock=False)


def test_same_id_twice_rejected():
    with pytest.raises(ValueError):
        ServoPair((43, 43), mock=True)


def test_context_manager_releases_torque():
    pair, wire = make_pair()
    with pair:
        pair.move_by(50)
    assert wire.regs[43][40] == 0 and wire.regs[13][40] == 0


def test_mock_mode_touches_nothing():
    pair = ServoPair((43, 13), mock=True)
    assert pair.move_by(100) is True
    pair.run_for(1.0)
    pair.close()
