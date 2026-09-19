"""Feetech STS/ST-series servo bus, spoken directly over a serial port.

This replaces the USB bus servo adapter board *and* the vendor SDK. The Pi's
own UART (GPIO14 TX / GPIO15 RX, `/dev/ttyAMA0` on a Pi 5) is wired straight
to the servo's one-wire data line:

    Pi GPIO14 (TX, pin 8)  --[ 1k ]--+
    Pi GPIO15 (RX, pin 10) ----------+---- servo DATA
    Pi GND ------------------------------- servo GND + battery -
    battery + ---------------------------- servo V+   (never to the Pi)

Because TX and RX share that one wire, every byte we send arrives back on RX
before the servo's reply does. This module is the "translation" the adapter
board used to do: it frames packets, swallows its own echo, and parses the
reply. The echo is also a free wiring self-test, which is why there are three
distinct failures instead of one generic timeout:

    BusWiringError      we sent bytes and heard nothing  -> RX isn't on the wire
    BusContentionError  we heard something else          -> the line is being fought over
    ServoTimeout        echo was perfect, servo was silent -> servo side (power, cable, ID, baud)

Packet format, both directions (low byte first for 16-bit values):

    FF FF | ID | LEN | INSTRUCTION or ERROR | PARAMS... | CHECKSUM
    LEN = len(PARAMS) + 2          CHECKSUM = ~(ID + LEN + INSTR + PARAMS) & 0xFF

Bring-up CLI (run from sorter/):

    python -m actuators.sts_bus loopback        # no servo needed: proves UART + TX/RX tie at full baud
    python -m actuators.sts_bus scan [--all]    # who is on the bus, and what do they report
    python -m actuators.sts_bus watch <id>      # torque off, live position - for recording poses
    python -m actuators.sts_bus move <id> <pos> # one move at config speed/acc
"""
from __future__ import annotations

import logging
import os
import sys
import time

log = logging.getLogger(__name__)

# --- Protocol constants ------------------------------------------------------
HEADER = b"\xff\xff"
BROADCAST_ID = 0xFE

INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03
INST_SYNC_WRITE = 0x83

# Register addresses (STS3215 control table)
ADDR_ID = 5
ADDR_MIN_ANGLE_LIMIT = 9      # 2 bytes, EEPROM
ADDR_MAX_ANGLE_LIMIT = 11     # 2 bytes, EEPROM
ADDR_MODE = 33                # 0 = position servo
ADDR_TORQUE_ENABLE = 40
ADDR_ACC = 41                 # start of the 7-byte block write_pos_ex writes
ADDR_GOAL_POSITION = 42
ADDR_TORQUE_LIMIT = 48        # 2 bytes, 0-1000
ADDR_LOCK = 55                # 0 = EEPROM writable, 1 = locked
ADDR_PRESENT_POSITION = 56    # start of the 15-byte feedback block

FEEDBACK_LEN = 15
POSITION_MAX = 4095
TICKS_PER_REV = 4096

# Status-packet error bits
ERROR_BITS = {0: "voltage", 1: "sensor", 2: "temperature", 3: "current", 5: "overload"}


class StsBusError(RuntimeError):
    """Base class for everything this module raises."""


class BusWiringError(StsBusError):
    """We transmitted and heard nothing back, not even our own echo."""


class BusContentionError(StsBusError):
    """What came back while we were transmitting wasn't what we sent."""


class ServoTimeout(StsBusError):
    """The bus is fine (echo intact) but the servo didn't answer."""

    def __init__(self, servo_id: int) -> None:
        super().__init__(f"servo {servo_id} did not reply (bus echo was fine - check servo power, cable, ID, baud)")
        self.servo_id = servo_id


class BadReply(StsBusError):
    """A reply arrived but failed validation (checksum, ID, or length)."""


def build_packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    body = bytes([servo_id, len(params) + 2, instruction]) + bytes(params)
    return HEADER + body + bytes([~sum(body) & 0xFF])


def _signed(value: int, sign_bit: int) -> int:
    """STS servos use sign-magnitude: one bit is the minus sign, the rest is the number."""
    magnitude = value & ((1 << sign_bit) - 1)
    return -magnitude if value & (1 << sign_bit) else magnitude


class StsBus:
    def __init__(
        self,
        port: str,
        baudrate: int = 1_000_000,
        reply_timeout_s: float = 0.05,
        echo: bool | None = None,
        serial_port=None,
    ) -> None:
        """Open the bus.

        echo: True = TX and RX share the wire (direct UART wiring); False = an
        adapter that hides the echo; None = find out by probing once.
        serial_port: an already-open pyserial-like object (used by the tests).
        """
        self.port = port
        self.reply_timeout_s = reply_timeout_s
        if serial_port is not None:
            self._ser = serial_port
        else:
            import serial  # imported here so mock mode / tests never need pyserial

            self._ser = serial.Serial(port, baudrate, timeout=0.005)
        self.echo = self._detect_echo() if echo is None else echo
        log.info("StsBus open on %s @ %d baud (echo=%s)", port, baudrate, self.echo)

    # --- low level -----------------------------------------------------------
    def _read_exact(self, count: int, timeout_s: float) -> bytes:
        got = bytearray()
        deadline = time.monotonic() + timeout_s
        while len(got) < count and time.monotonic() < deadline:
            got += self._ser.read(count - len(got))
        return bytes(got)

    def _detect_echo(self) -> bool:
        probe = build_packet(BROADCAST_ID, INST_PING)  # harmless: moves nothing
        self._ser.reset_input_buffer()
        self._ser.write(probe)
        self._ser.flush()
        heard = self._read_exact(len(probe), 0.05)
        time.sleep(0.01)  # let any servo's answer to the probe arrive, then drop it
        self._ser.reset_input_buffer()
        return heard == probe

    def _send(self, packet: bytes) -> None:
        self._ser.reset_input_buffer()
        self._ser.write(packet)
        self._ser.flush()
        if not self.echo:
            return
        heard = self._read_exact(len(packet), 0.05)
        if not heard:
            raise BusWiringError(
                f"sent {len(packet)} bytes on {self.port} and heard nothing - RX is not seeing TX "
                "(check the TX/RX tie, and that the UART is enabled)"
            )
        if heard != packet:
            raise BusContentionError(
                f"sent {packet.hex(' ')} but heard {heard.hex(' ')} - something else is driving the data line"
            )

    def _read_reply(self, servo_id: int, timeout_s: float) -> tuple[int, bytes]:
        """Returns (error_byte, params). Raises ServoTimeout / BadReply."""
        head = self._read_exact(4, timeout_s)  # FF FF ID LEN
        if len(head) < 4:
            raise ServoTimeout(servo_id)
        if head[:2] != HEADER:
            raise BadReply(f"servo {servo_id}: reply did not start with FF FF: {head.hex(' ')}")
        reply_id, length = head[2], head[3]
        rest = self._read_exact(length, timeout_s)  # ERROR PARAMS... CHECKSUM
        if len(rest) < length:
            raise BadReply(f"servo {servo_id}: reply cut short: {(head + rest).hex(' ')}")
        if ~(reply_id + length + sum(rest[:-1])) & 0xFF != rest[-1]:
            raise BadReply(f"servo {servo_id}: bad checksum: {(head + rest).hex(' ')}")
        if reply_id != servo_id:
            raise BadReply(f"asked servo {servo_id} but servo {reply_id} answered")
        return rest[0], rest[1:-1]

    def _transact(self, servo_id: int, instruction: int, params: bytes = b"", timeout_s: float | None = None):
        self._send(build_packet(servo_id, instruction, params))
        if servo_id == BROADCAST_ID:
            return 0, b""  # nobody answers a broadcast write
        return self._read_reply(servo_id, self.reply_timeout_s if timeout_s is None else timeout_s)

    # --- public API ------------------------------------------------------------
    def ping(self, servo_id: int, timeout_s: float | None = None) -> bool:
        """True if the servo answered. Wiring/contention problems still raise."""
        try:
            self._transact(servo_id, INST_PING, timeout_s=timeout_s)
            return True
        except (ServoTimeout, BadReply):
            return False

    def scan(self, ids=range(0, 31)) -> list[int]:
        return [sid for sid in ids if self.ping(sid, timeout_s=0.01)]

    def read(self, servo_id: int, address: int, length: int) -> bytes:
        _error, params = self._transact(servo_id, INST_READ, bytes([address, length]))
        if len(params) != length:
            raise BadReply(f"servo {servo_id}: asked for {length} bytes at {address}, got {len(params)}")
        return params

    def read_byte(self, servo_id: int, address: int) -> int:
        return self.read(servo_id, address, 1)[0]

    def read_word(self, servo_id: int, address: int) -> int:
        data = self.read(servo_id, address, 2)
        return data[0] | (data[1] << 8)

    def write(self, servo_id: int, address: int, data: bytes) -> int:
        """Write registers. Returns the servo's error byte (0 = healthy)."""
        error, _params = self._transact(servo_id, INST_WRITE, bytes([address]) + bytes(data))
        return error

    def write_byte(self, servo_id: int, address: int, value: int) -> int:
        return self.write(servo_id, address, bytes([value & 0xFF]))

    def write_word(self, servo_id: int, address: int, value: int) -> int:
        return self.write(servo_id, address, bytes([value & 0xFF, (value >> 8) & 0xFF]))

    def set_torque(self, servo_id: int, enabled: bool) -> int:
        return self.write_byte(servo_id, ADDR_TORQUE_ENABLE, 1 if enabled else 0)

    def angle_limits(self, servo_id: int) -> tuple[int, int]:
        """(min, max) goal position the servo will accept. It clamps anything outside."""
        return self.read_word(servo_id, ADDR_MIN_ANGLE_LIMIT), self.read_word(servo_id, ADDR_MAX_ANGLE_LIMIT)

    def set_angle_limits(self, servo_id: int, lower: int, upper: int) -> None:
        """Write the limits to EEPROM (unlock, write, lock). EEPROM has a finite
        write life, so this skips the write when the stored values already match."""
        if self.angle_limits(servo_id) == (lower, upper):
            return
        self.write_byte(servo_id, ADDR_LOCK, 0)
        try:
            self.write_word(servo_id, ADDR_MIN_ANGLE_LIMIT, lower)
            self.write_word(servo_id, ADDR_MAX_ANGLE_LIMIT, upper)
        finally:
            self.write_byte(servo_id, ADDR_LOCK, 1)

    @staticmethod
    def _pos_ex_block(position: int, speed: int, acc: int) -> bytes:
        """The 7 bytes at ADDR_ACC: acc, position lo/hi, time 0/0, speed lo/hi."""
        position = max(0, min(POSITION_MAX, int(position)))
        return bytes([acc & 0xFF, position & 0xFF, position >> 8, 0, 0, speed & 0xFF, (speed >> 8) & 0xFF])

    def write_pos_ex(self, servo_id: int, position: int, speed: int, acc: int = 0) -> int:
        """Go to position (0-4095). speed in steps/s (0 = no cap), acc in 100 steps/s^2 (0 = no ramp)."""
        return self.write(servo_id, ADDR_ACC, self._pos_ex_block(position, speed, acc))

    def sync_write_pos_ex(self, moves: list[tuple[int, int, int, int]]) -> None:
        """Start several servos in one packet. moves = [(id, position, speed, acc), ...]. No replies."""
        params = bytearray([ADDR_ACC, 7])
        for servo_id, position, speed, acc in moves:
            params.append(servo_id)
            params += self._pos_ex_block(position, speed, acc)
        self._transact(BROADCAST_ID, INST_SYNC_WRITE, bytes(params))

    def feedback(self, servo_id: int) -> dict:
        """Everything the servo reports about itself, in one read."""
        d = self.read(servo_id, ADDR_PRESENT_POSITION, FEEDBACK_LEN)
        status = d[9]
        return {
            "position": _signed(d[0] | (d[1] << 8), 15),          # ticks
            "speed": _signed(d[2] | (d[3] << 8), 15),             # steps/s
            "load": _signed(d[4] | (d[5] << 8), 10) / 10.0,       # % of max, sign = direction
            "voltage": d[6] / 10.0,                               # V
            "temperature": d[7],                                  # C
            "moving": bool(d[10]),
            "current_ma": _signed(d[13] | (d[14] << 8), 15) * 6.5,
            "errors": [name for bit, name in ERROR_BITS.items() if status & (1 << bit)],
        }

    def close(self) -> None:
        self._ser.close()


# --- bring-up CLI ----------------------------------------------------------------
def _loopback(port: str, baudrate: int) -> int:
    import serial

    # No 0xFF bytes: a servo that happens to be attached can never see a packet header.
    blob = bytes(b % 0xFF for b in os.urandom(200))
    with serial.Serial(port, baudrate, timeout=0.5) as ser:
        ser.reset_input_buffer()
        ser.write(blob)
        ser.flush()
        heard = ser.read(len(blob))
    print(f"{port} @ {baudrate}: sent {len(blob)} bytes, heard {len(heard)}, identical={heard == blob}")
    if heard == blob:
        print("OK - the UART works at this baud and RX sees TX. Safe to connect the servo.")
        return 0
    if not heard:
        print("Heard nothing: RX isn't connected to the data wire, or this isn't the header UART.")
    else:
        print("Heard different bytes: bad connection, or something else is driving the line.")
    return 1


def _cli(argv: list[str]) -> int:
    import argparse

    import config

    ap = argparse.ArgumentParser(prog="python -m actuators.sts_bus", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=config.SERVO_PORT)
    ap.add_argument("--baud", type=int, default=config.SERVO_BAUDRATE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("loopback")
    p = sub.add_parser("scan")
    p.add_argument("--all", action="store_true", help="IDs 0-253 instead of 0-50")
    p = sub.add_parser("watch")
    p.add_argument("id", type=int)
    p = sub.add_parser("move")
    p.add_argument("id", type=int)
    p.add_argument("position", type=int)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "loopback":
            return _loopback(args.port, args.baud)
        bus = StsBus(args.port, args.baud)
    except OSError as e:  # pyserial's SerialException is an OSError
        print(f"Could not open {args.port}: {e}")
        return 1
    try:
        print(f"{args.port} @ {args.baud}, echo={'yes' if bus.echo else 'no'}")
        if args.cmd == "scan":
            ids = range(0, 254) if args.all else range(0, 51)
            found = bus.scan(ids)
            if not found:
                print(f"No servo answered on IDs {ids[0]}-{ids[-1]}.")
                return 1
            for sid in found:
                fb = bus.feedback(sid)
                print(f"ID {sid}: pos {fb['position']} ({fb['position'] * 360 / TICKS_PER_REV:.1f} deg), "
                      f"{fb['voltage']:.1f} V, {fb['temperature']} C, mode {bus.read_byte(sid, ADDR_MODE)}, "
                      f"limits [{bus.read_word(sid, ADDR_MIN_ANGLE_LIMIT)}, {bus.read_word(sid, ADDR_MAX_ANGLE_LIMIT)}], "
                      f"errors {fb['errors'] or 'none'}")
        elif args.cmd == "watch":
            bus.set_torque(args.id, False)
            print(f"Servo {args.id}: torque off, move it by hand. Ctrl+C to exit.")
            try:
                while True:
                    pos = bus.feedback(args.id)["position"]
                    print(f"\rpos {pos:5d} ticks  ({pos * 360 / TICKS_PER_REV:6.1f} deg)   ", end="", flush=True)
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print()
        elif args.cmd == "move":
            bus.set_torque(args.id, True)
            error = bus.write_pos_ex(args.id, args.position, config.SERVO_MOVE_SPEED, config.SERVO_MOVE_ACC)
            print(f"servo {args.id} -> {args.position} (error byte {error})")
        return 0
    except StsBusError as e:
        print(f"{type(e).__name__}: {e}")
        return 1
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
