"""STS3215 smart servo wrapper (servo IDs from config), driven directly from
the Pi's UART through actuators/sts_bus.py - no bus adapter board and no
vendor SDK in between. See sts_bus.py for the wiring and the protocol.

Real mode opens the serial port, checks every configured servo answers a
ping, and enables torque. Mock mode just logs the intended move instead of
opening a serial port, so this module is safe to exercise from a laptop with
nothing plugged in.

SERVO_ID_SAFE_GATE and SERVO_ID_FLAGGED_GATE may be the same ID: that is the
single-servo build, where one servo tilts the bed either way from home.

For hardware bring-up use the bus tools first (README, "Servo bring-up"):
`python -m actuators.sts_bus loopback`, then `scan`, before running this.
"""
from __future__ import annotations

import logging
import time

import config

log = logging.getLogger(__name__)


class ServoController:
    def __init__(self, mock: bool | None = None) -> None:
        self.mock = config.MOCK_HARDWARE if mock is None else mock
        self._bus = None
        # dict.fromkeys keeps order and drops the duplicate in the single-servo build
        self._ids = list(dict.fromkeys([config.SERVO_ID_SAFE_GATE, config.SERVO_ID_FLAGGED_GATE]))

        if self.mock:
            log.info("ServoController running in MOCK mode (no serial port opened)")
            return

        from actuators.sts_bus import StsBus

        self._bus = StsBus(config.SERVO_PORT, config.SERVO_BAUDRATE)

        missing = [sid for sid in self._ids if not self._bus.ping(sid)]
        if missing:
            self._bus.close()
            raise RuntimeError(
                f"servo ID(s) {missing} did not answer on {config.SERVO_PORT} "
                "(run `python -m actuators.sts_bus scan --all` to see what is on the bus)"
            )
        for sid in self._ids:
            self._bus.set_torque(sid, True)

        log.info("ServoController ready on %s, servo IDs %s", config.SERVO_PORT, self._ids)

    def _move(self, servo_id: int, position: int) -> None:
        if self.mock:
            log.info("[MOCK] servo %d -> position %d", servo_id, position)
            return

        error = self._bus.write_pos_ex(servo_id, position, config.SERVO_MOVE_SPEED, config.SERVO_MOVE_ACC)
        if error:
            log.warning("servo %d reported error byte 0x%02x", servo_id, error)
        log.info("servo %d -> position %d", servo_id, position)

    def _tilt(self, active_id: int, position: int) -> None:
        """Send one servo to `position`, and any other servo back to home."""
        self._move(active_id, position)
        for sid in self._ids:
            if sid != active_id:
                self._move(sid, config.SERVO_HOME_POSITION)
        time.sleep(config.SERVO_MOVE_SETTLE_S)

    def home(self) -> None:
        """Return every servo to the neutral/home position."""
        for sid in self._ids:
            self._move(sid, config.SERVO_HOME_POSITION)
        time.sleep(config.SERVO_MOVE_SETTLE_S)

    def move_safe(self) -> None:
        """Tilt the item into the 'safe' bin."""
        self._tilt(config.SERVO_ID_SAFE_GATE, config.SERVO_SAFE_POSITION)

    def move_flagged(self) -> None:
        """Tilt the item into the 'flagged' (likely battery) bin."""
        self._tilt(config.SERVO_ID_FLAGGED_GATE, config.SERVO_FLAGGED_POSITION)

    def close(self) -> None:
        if self.mock or self._bus is None:
            return
        try:
            self.home()
            for sid in self._ids:
                self._bus.set_torque(sid, False)
        finally:
            self._bus.close()
            self._bus = None
            log.info("Servo serial port closed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"MOCK_HARDWARE={config.MOCK_HARDWARE}")
    ctrl = ServoController()
    try:
        print("Homing...")
        ctrl.home()
        print("Moving to SAFE...")
        ctrl.move_safe()
        time.sleep(1)
        print("Moving to FLAGGED...")
        ctrl.move_flagged()
        time.sleep(1)
        print("Homing...")
        ctrl.home()
    finally:
        ctrl.close()
