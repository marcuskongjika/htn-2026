"""Real-webcam capture + mocked-Gemini smoke test.

Captures ONE frame from THIS computer's webcam (real OpenCV capture), verifies
the bytes are a valid JPEG, then runs the material classifier in MOCK mode (no
Gemini network call, no API key). This proves the real-camera -> classifier
path end to end without spending API quota.

    cd sorter
    python -m tests.webcam_smoke              # webcam index 0 (default cam)
    python -m tests.webcam_smoke --index 1    # a different camera
    python -m tests.webcam_smoke --real-gemini  # also hit the real Gemini API

NOTE (WSL2): OpenCV inside WSL2 cannot see the Windows webcam unless the USB
device is attached with usbipd-win AND the WSL kernel has UVC/V4L2 support. On
a stock WSL2 install there is no /dev/video* and capture fails with a clear
message. In that case, run this under Windows Python (python.exe) instead,
where the built-in webcam is reachable.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from vision.camera import capture_frame
from vision.classifier import classify_material

JPEG_MAGIC = b"\xff\xd8\xff"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=config.CAMERA_INDEX,
                        help="camera index to open (default: config.CAMERA_INDEX)")
    parser.add_argument("--out", default="capture_test.jpg",
                        help="where to save the captured JPEG")
    parser.add_argument("--real-gemini", action="store_true",
                        help="send the frame to the real Gemini API instead of mocking it")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Force a REAL capture from the chosen camera, regardless of MOCK_HARDWARE.
    config.CAMERA_INDEX = args.index
    print(f"Capturing from webcam index {args.index} (real capture) ...")
    try:
        frame = capture_frame(mock=False)
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"\nERROR: could not capture from the webcam: {exc}\n", file=sys.stderr)
        print(
            "Troubleshooting:\n"
            "  - No /dev/video* on WSL2 is expected: OpenCV in WSL2 can't reach the\n"
            "    Windows webcam. Run this under Windows Python (python.exe), or attach\n"
            "    the USB camera with usbipd-win + a UVC-capable WSL kernel.\n"
            "  - Try a different --index (0, 1, 2 ...).\n"
            "  - Make sure no other app is holding the camera.\n"
            "  - Ensure opencv-python is installed in this interpreter.",
            file=sys.stderr,
        )
        return 1

    # Verify the bytes really are a usable JPEG for Gemini.
    is_jpeg = frame.startswith(JPEG_MAGIC)
    print(f"Captured {len(frame)} bytes; valid JPEG header: {is_jpeg}")
    if not is_jpeg:
        print("WARNING: captured bytes are not a JPEG — Gemini expects image/jpeg.", file=sys.stderr)

    out_path = Path(args.out)
    out_path.write_bytes(frame)
    print(f"Saved frame -> {out_path.resolve()}")

    mode = "REAL Gemini" if args.real_gemini else "MOCK Gemini"
    print(f"Classifying with {mode} ...")
    result = classify_material(frame, mock=not args.real_gemini)
    print("Classification result:")
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
