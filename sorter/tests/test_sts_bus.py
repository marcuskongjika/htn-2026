"""Unit tests for actuators.sts_bus - no hardware: a fake serial port plays
both the shared wire (echoing what we send) and one servo on it.

Run with:
    cd sorter
    python -m pytest tests/test_sts_bus.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actuators.sts_bus import (
    ADDR_ACC,
    ADDR_PRESENT_POSITION,
    ADDR_TORQUE_ENABLE,
    BadReply,
    BusContentionError,
    BusWiringError,
    ServoTimeout,
    StsBus,
    build_packet,
)


class FakeWire:
    """pyserial-shaped object. echo=True models TX and RX tied to one wire."""

    def __init__(self, servo_id=None, echo=True, corrupt_echo=False, corrupt_checksum=False, answer_as=None):
        self.servo_id = servo_id
        self.echo = echo
        self.corrupt_echo = corrupt_echo
        self.corrupt_checksum = corrupt_checksum
        self.answer_as = answer_as  # reply with a different ID than the one addressed
        self.registers = bytearray(128)
        self.rx = bytearray()
        self.sent = []

    # --- pyserial surface ---
    def write(self, data):
        data = bytes(data)
        self.sent.append(data)
        if self.echo:
            self.rx += bytes([data[0] ^ 0x01]) + data[1:] if self.corrupt_echo else data
        self.rx += self._servo(data)
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

    # --- the servo on the wire ---
    def _servo(self, packet):
        if self.servo_id is None or packet[:2] != b"\xff\xff" or packet[2] != self.servo_id:
            return b""
        instruction, params = packet[4], packet[5:-1]
        if instruction == 0x01:
            payload = b""
        elif instruction == 0x02:
            payload = bytes(self.registers[params[0]:params[0] + params[1]])
        elif instruction == 0x03:
            self.registers[params[0]:params[0] + len(params) - 1] = params[1:]
            payload = b""
        else:
            return b""
        reply = bytearray(build_packet(self.answer_as or self.servo_id, 0x00, payload))  # 0x00 = no error
        if self.corrupt_checksum:
            reply[-1] ^= 0xFF
        return bytes(reply)


def make_bus(**wire_kwargs):
    echo = wire_kwargs.pop("bus_echo", True)
    wire = FakeWire(**wire_kwargs)
    return StsBus("fake", serial_port=wire, echo=echo, reply_timeout_s=0.01), wire


# --- known-good byte vectors (same bytes the C++ SCServo library sends) -------
def test_ping_packet_bytes():
    assert build_packet(1, 0x01) == bytes.fromhex("FF FF 01 02 01 FB")


def test_healthy_ping_reply_bytes():
    assert build_packet(1, 0x00) == bytes.fromhex("FF FF 01 02 00 FC")


def test_write_pos_ex_bytes():
    bus, wire = make_bus(servo_id=1)
    bus.write_pos_ex(1, 2048, speed=1000, acc=50)
    # addr 41 | acc 50 | pos 0x0800 lo,hi | time 0,0 | speed 1000 = 0x03E8 lo,hi
    assert wire.sent[-1] == bytes.fromhex("FF FF 01 0A 03 29 32 00 08 00 00 E8 03 A3")


def test_write_pos_ex_clamps_position():
    bus, wire = make_bus(servo_id=1)
    bus.write_pos_ex(1, 9999, speed=100)
    assert wire.registers[ADDR_ACC + 1] | (wire.registers[ADDR_ACC + 2] << 8) == 4095


def test_sync_write_bytes_and_no_reply_expected():
    bus, wire = make_bus(servo_id=1)
    bus.sync_write_pos_ex([(1, 2048, 1000, 50), (2, 1024, 1000, 50)])
    sent = wire.sent[-1]
    assert sent[:7] == bytes.fromhex("FF FF FE 14 83 29 07")
    assert sent[7:15] == bytes.fromhex("01 32 00 08 00 00 E8 03")
    assert sent[15:23] == bytes.fromhex("02 32 00 04 00 00 E8 03")


# --- echo handling + replies ---------------------------------------------------
def test_ping_strips_echo_and_sees_servo():
    bus, _ = make_bus(servo_id=43)
    assert bus.ping(43) is True


def test_echo_alone_is_not_a_servo():
    # The exact trap the vendor SDKs fell into on real hardware: the echo of a
    # ping looks like a status packet. With no servo there, ping must be False.
    bus, _ = make_bus(servo_id=None)
    assert bus.ping(1) is False
    with pytest.raises(ServoTimeout):
        bus.read(1, ADDR_PRESENT_POSITION, 2)


def test_other_ids_do_not_answer():
    bus, _ = make_bus(servo_id=43)
    assert bus.scan(range(40, 46)) == [43]


def test_no_echo_means_wiring_error():
    # RX not connected to the data wire: it hears neither our echo nor any servo.
    bus, _ = make_bus(servo_id=None, echo=False)
    with pytest.raises(BusWiringError):
        bus.ping(1)


def test_corrupted_echo_means_contention():
    bus, _ = make_bus(servo_id=1, corrupt_echo=True)
    with pytest.raises(BusContentionError):
        bus.ping(1)


def test_works_without_echo_when_told_so():
    bus, _ = make_bus(servo_id=1, echo=False, bus_echo=False)  # e.g. a USB adapter that hides it
    assert bus.ping(1) is True


def test_echo_autodetect():
    assert StsBus("fake", serial_port=FakeWire(echo=True), echo=None).echo is True
    assert StsBus("fake", serial_port=FakeWire(echo=False), echo=None).echo is False


def test_bad_checksum_rejected():
    bus, _ = make_bus(servo_id=1, corrupt_checksum=True)
    with pytest.raises(BadReply):
        bus.read(1, ADDR_PRESENT_POSITION, 2)
    assert bus.ping(1) is False


def test_reply_from_wrong_id_rejected():
    bus, _ = make_bus(servo_id=1, answer_as=2)
    with pytest.raises(BadReply):
        bus.read(1, ADDR_PRESENT_POSITION, 2)


# --- register access -------------------------------------------------------------
def test_write_then_read_back():
    bus, wire = make_bus(servo_id=7)
    assert bus.set_torque(7, True) == 0
    assert wire.registers[ADDR_TORQUE_ENABLE] == 1
    assert bus.read_byte(7, ADDR_TORQUE_ENABLE) == 1
    bus.write_word(7, 9, 1650)
    assert bus.read_word(7, 9) == 1650


def test_feedback_decoding():
    bus, wire = make_bus(servo_id=1)
    block = bytearray(15)
    block[0:2] = (2048).to_bytes(2, "little")             # position
    block[2:4] = (0x8000 | 300).to_bytes(2, "little")     # speed -300 (bit 15 = minus)
    block[4:6] = (0x400 | 250).to_bytes(2, "little")      # load -25.0 % (bit 10 = minus)
    block[6] = 74                                          # 7.4 V
    block[7] = 31                                          # 31 C
    block[9] = 0b00100001                                  # voltage + overload flags
    block[10] = 1                                          # moving
    block[13:15] = (100).to_bytes(2, "little")            # 100 * 6.5 mA
    wire.registers[ADDR_PRESENT_POSITION:ADDR_PRESENT_POSITION + 15] = block

    fb = bus.feedback(1)
    assert fb["position"] == 2048
    assert fb["speed"] == -300
    assert fb["load"] == -25.0
    assert fb["voltage"] == 7.4
    assert fb["temperature"] == 31
    assert fb["moving"] is True
    assert fb["current_ma"] == 650.0
    assert fb["errors"] == ["voltage", "overload"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
