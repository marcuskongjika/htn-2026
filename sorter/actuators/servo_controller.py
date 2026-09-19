"""STS3215 smart servo wrapper (two servos, IDs from config), driven over
USB via a bus servo adapter using the Feetech/Waveshare STServo SDK
(distributed as `feetech-servo-sdk` or `st3215` on PyPI, both of which
expose the same underlying `STservo_sdk` module: `PortHandler` for the
serial link and `sts` as the ST-protocol packet handler).

Real mode opens the serial port and drives both servos. Mock mode just logs
the intended move instead of opening a serial port, so this module is safe
to exercise from a laptop with nothing plugged in.

NOTE for hardware bring-up: verify `WritePosEx`/`ReadPos` argument order
against whichever SDK version actually installs (`pip show feetech-servo-sdk`
or `st3215`) — vendor SDK method signatures have drifted across releases.
Do this during the "bring up both servos over USB" bring-up step in the
README before trusting move_safe()/move_flagged() on real hardware.
"""
from __future__ import annotations

import logging
import time

import config

log = logging.getLogger(__name__)


class ServoController:
    def __init__(self, mock: bool | None = None) -> None:
        self.mock = config.MOCK_HARDWARE if mock is None else mock
        self._port_handler = None
        self._packet_handler = None

        if self.mock:
            log.info("ServoController running in MOCK mode (no serial port opened)")
            return

        from STservo_sdk import PortHandler, sts  # type: ignore[import-not-found]

        self._port_handler = PortHandler(config.SERVO_PORT)
        self._packet_handler = sts(self._port_handler)

        if not self._port_handler.openPort():
            raise RuntimeError(f"Failed to open servo serial port {config.SERVO_PORT}")
        if not self._port_handler.setBaudRate(config.SERVO_BAUDRATE):
            raise RuntimeError(f"Failed to set baud rate {config.SERVO_BAUDRATE}")

        log.info(
            "ServoController opened %s @ %d baud for servo IDs %d, %d",
            config.SERVO_PORT,
            config.SERVO_BAUDRATE,
            config.SERVO_ID_SAFE_GATE,
            config.SERVO_ID_FLAGGED_GATE,
        )

    def _move(self, servo_id: int, position: int) -> None:
        if self.mock:
            log.info("[MOCK] servo %d -> position %d", servo_id, position)
            return

        result, error = self._packet_handler.WritePosEx(
            servo_id, position, config.SERVO_MOVE_SPEED, 0
        )
        if result != 0:
            raise RuntimeError(
                f"servo {servo_id} write failed: comm result={result} error={error}"
            )
        log.info("servo %d -> position %d", servo_id, position)

    def home(self) -> None:
        """Return both servos to the neutral/home position."""
        self._move(config.SERVO_ID_SAFE_GATE, config.SERVO_HOME_POSITION)
        self._move(config.SERVO_ID_FLAGGED_GATE, config.SERVO_HOME_POSITION)
        time.sleep(config.SERVO_MOVE_SETTLE_S)

    def move_safe(self) -> None:
        """Tilt the item into the 'safe' bin."""
        self._move(config.SERVO_ID_SAFE_GATE, config.SERVO_SAFE_POSITION)
        self._move(config.SERVO_ID_FLAGGED_GATE, config.SERVO_HOME_POSITION)
        time.sleep(config.SERVO_MOVE_SETTLE_S)

    def move_flagged(self) -> None:
        """Tilt the item into the 'flagged' (likely battery) bin."""
        self._move(config.SERVO_ID_FLAGGED_GATE, config.SERVO_FLAGGED_POSITION)
        self._move(config.SERVO_ID_SAFE_GATE, config.SERVO_HOME_POSITION)
        time.sleep(config.SERVO_MOVE_SETTLE_S)

    def close(self) -> None:
        if not self.mock and self._port_handler is not None:
            self._port_handler.closePort()
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
