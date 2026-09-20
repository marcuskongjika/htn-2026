"""tests.move_pair against two simulated servos sharing one wire.

Run with:
    cd sorter
    python -m pytest tests/test_move_pair.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators.sts_bus import StsBus, build_packet
from tests import move_pair


class PairWire:
    """One shared wire (echoes everything) with several servos on it. Servos jump
    straight to their goal, clamped to their stored angle limits like real ones."""

    def __init__(self, positions: dict[int, int], limits: dict[int, tuple[int, int]] | None = None):
        self.regs = {sid: bytearray(128) for sid in positions}
        for sid, pos in positions.items():
            self._set(sid, 56, pos)
            lower, upper = (limits or {}).get(sid, (0, 4095))
            self._set(sid, 9, lower)
            self._set(sid, 11, upper)
        self.rx = bytearray()
        self.sync_packets = []
        self.goals = {sid: [] for sid in positions}

    def _set(self, sid, address, value):
        self.regs[sid][address:address + 2] = value.to_bytes(2, "little")

    def word(self, sid, address):
        return int.from_bytes(self.regs[sid][address:address + 2], "little")

    def _apply(self, sid, address, data):
        self.regs[sid][address:address + len(data)] = data
        if address <= 42 < address + len(data):  # a goal position was written
            goal, lower, upper = self.word(sid, 42), self.word(sid, 9), self.word(sid, 11)
            if not (lower == 0 and upper == 0):
                goal = min(max(goal, lower), upper)
            self.goals[sid].append(goal)
            self._set(sid, 56, goal)

    # --- pyserial surface ---
    def write(self, data):
        data = bytes(data)
        self.rx += data  # echo
        sid, instruction, params = data[2], data[4], data[5:-1]
        if sid == 0xFE and instruction == 0x83:
            self.sync_packets.append(data)
            address, size = params[0], params[1]
            for i in range(2, len(params), size + 1):
                if params[i] in self.regs:
                    self._apply(params[i], address, params[i + 1:i + 1 + size])
        elif sid in self.regs:
            payload = b""
            if instruction == 0x02:
                payload = bytes(self.regs[sid][params[0]:params[0] + params[1]])
            elif instruction == 0x03:
                self._apply(sid, params[0], params[1:])
            self.rx += build_packet(sid, 0x00, payload)
        return len(data)

    def read(self, count):
        out, self.rx = bytes(self.rx[:count]), self.rx[count:]
        return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    def _run(wire, *argv):
        monkeypatch.setattr(move_pair, "StsBus", lambda port, baud: StsBus(port, serial_port=wire, echo=True, reply_timeout_s=0.01))
        return move_pair.main([*argv, "--yes"])
    return _run


def test_both_move_the_same_way_and_return(run):
    wire = PairWire({43: 2053, 13: 2840})
    assert run(wire) == 0
    assert wire.goals[43] == [2153, 2053]
    assert wire.goals[13] == [2940, 2840]
    assert wire.regs[43][40] == 0 and wire.regs[13][40] == 0   # torque released


def test_invert_13_mirrors_the_follower(run):
    wire = PairWire({43: 2053, 13: 2840})
    assert run(wire, "--invert-13") == 0
    assert wire.goals[43] == [2153, 2053]
    assert wire.goals[13] == [2740, 2840]


def test_invert_with_negative_ticks(run):
    wire = PairWire({43: 2053, 13: 2840})
    assert run(wire, "--invert-13", "--ticks", "-200", "--stay") == 0
    assert wire.goals[43] == [1853]
    assert wire.goals[13] == [3040]


def test_both_goals_travel_in_one_packet(run):
    wire = PairWire({43: 2053, 13: 2840})
    run(wire, "--stay")
    assert len(wire.sync_packets) == 1
    ids_in_packet = {wire.sync_packets[0][7], wire.sync_packets[0][15]}
    assert ids_in_packet == {43, 13}


def test_configured_torque_limit_applied_to_both(run):
    import config
    wire = PairWire({43: 2053, 13: 2840})
    run(wire)
    assert wire.word(43, 48) == config.SERVO_TORQUE_LIMIT and wire.word(13, 48) == config.SERVO_TORQUE_LIMIT
    run(wire, "--torque", "450")
    assert wire.word(43, 48) == 450


def test_refuses_if_either_servo_would_clamp(run, capsys):
    wire = PairWire({43: 2053, 13: 2840}, limits={13: (2964, 3697)})
    assert run(wire) == 2
    assert wire.goals == {43: [], 13: []}                       # neither moved
    assert "--open-limits" in capsys.readouterr().out


def test_refuses_if_one_is_missing_or_move_too_big(run):
    assert run(PairWire({43: 2053})) == 1                       # 13 not on the bus
    assert run(PairWire({43: 2053, 13: 2840}), "--ticks", "2000") == 2
