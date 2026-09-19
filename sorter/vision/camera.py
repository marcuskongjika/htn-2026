"""Camera capture wrapper.

Real mode grabs a single frame from a USB/CSI camera via OpenCV
(cv2.VideoCapture) and encodes it as JPEG bytes. Mock mode returns a small
bundled placeholder JPEG so the rest of the pipeline can be exercised with
no camera attached.

Standalone bring-up:
    cd sorter
    python -m vision.camera
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger(__name__)


def capture_frame(mock: bool | None = None) -> bytes:
    """Capture a single frame and return it as JPEG-encoded bytes."""
    use_mock = config.MOCK_HARDWARE if mock is None else mock

    if use_mock:
        log.info("Camera MOCK mode: returning bundled placeholder JPEG")
        return config.MOCK_CAMERA_PLACEHOLDER_JPEG.read_bytes()

    import cv2

    cap = cv2.VideoCapture(config.CAMERA_INDEX)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera at index {config.CAMERA_INDEX}")

    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("Camera returned no frame")

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), config.CAMERA_JPEG_QUALITY]
        ok, buf = cv2.imencode(".jpg", frame, encode_params)
        if not ok:
            raise RuntimeError("Failed to JPEG-encode captured frame")

        log.info("Captured frame from camera index %d (%d bytes)", config.CAMERA_INDEX, len(buf))
        return buf.tobytes()
    finally:
        cap.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"MOCK_HARDWARE={config.MOCK_HARDWARE}")
    jpeg_bytes = capture_frame()
    out_path = "capture_test.jpg"
    with open(out_path, "wb") as f:
        f.write(jpeg_bytes)
    print(f"Captured {len(jpeg_bytes)} bytes -> {out_path}")
