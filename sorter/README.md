# Battery Sorter

Raspberry Pi 5 pipeline that flags waste items likely to contain a hidden
battery. A load cell (HX711) and an inductive metal sensor cross-check a

Gemini photo classification of the item (whether it is plastic, and whether it
likely contains a hidden battery), then drive two STS3215 smart servos to tilt
the item into the "plastic" bin or the "other" bin. Clean plastic (Gemini says
plastic AND no metal detected) goes one way; everything else goes the other.


The pipeline is an explicit state machine (`main.py`) gated by the load cell —
nothing is captured until an item's weight is sensed:

```
WAITING -> (weight >= WEIGHT_TRIGGER_G) -> SENSING -> ACTUATING -> HOLDING -> RESETTING -> WAITING
   '-------------------- (below trigger: keep polling) --------------------'
```

WAITING polls the load cell. When an item's weight crosses `WEIGHT_TRIGGER_G`,
SENSING lets it settle, reads the metal sensor, and classifies the camera frame;
the bed then tilts (plastic with no metal -> min; everything else -> max), holds
for `TILT_HOLD_S` (default 5 s), returns to level, and waits for the next item.
On real hardware the load cell tares at start-up and after each dump.

Every hardware-touching module (`sensors/`, `vision/camera.py`,
`actuators/`) has a **mock mode** that returns synthetic data instead of
touching real GPIO/serial/camera. This is not a fallback bolted on — it's
how the whole pipeline is meant to be developed and demoed before hardware
is available.

## Install

```bash
cd sorter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`RPi.GPIO` only installs on ARM (the Pi itself); on a laptop, `pip install`
will skip it via the platform marker in `requirements.txt`, and mock mode
never imports it anyway.

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set:

```
GEMINI_API_KEY=your-real-key-here
MOCK_HARDWARE=1   # 1 on a laptop with no hardware, 0 on the Pi with hardware attached
```

`.env` is gitignored — never commit real keys.

## Run in mock mode (no hardware attached)

With `MOCK_HARDWARE=1` (the default), the whole pipeline — including a
canned Gemini-shaped response, no network call — runs end to end on a
laptop. The mock load cell's baseline weight sits above `WEIGHT_TRIGGER_G`, so
it fires every loop; it runs until Ctrl+C:

```bash
python main.py                                # runs until Ctrl+C
TILT_HOLD_S=1 python main.py --cycles 2       # bounded demo: 2 cycles, short hold
```

`--cycles N` stops after N completed cycles; `TILT_HOLD_S` tunes the hold at the
tilt. Every state transition and sensor/decision value is logged to stdout and
to `logs/sorter.log` (rotating, see `config.LOG_MAX_BYTES` /
`config.LOG_BACKUP_COUNT`).

To test just the material classifier against the real Gemini API while
everything else stays mocked, set `MOCK_HARDWARE=0` but note that will also
try to open the camera/GPIO/serial — see the bring-up order below for how
to exercise modules individually instead.

Run the decision-logic unit tests any time (pure function, no mocking
needed):

```bash
python -m pytest tests/test_decision.py -v
```

## Testing

All of these run without hardware. Activate the venv first (`source .venv/bin/activate`).

**Mock Gemini + decision unit tests** (offline, no API key, no camera):

```bash
python -m pytest tests/test_classifier.py tests/test_decision.py -v
```

`test_classifier.py` mocks Gemini two ways: the built-in canned-response path,
and a *fake `google.genai` SDK* injected into `sys.modules` so the real
request-build / JSON-parse code in `classify_material` runs offline. It also
covers fail-toward-"flagged" behavior on malformed output, errors, and timeouts.

**Webcam smoke test** (real camera capture + *mocked* Gemini) — captures one
frame from this machine's webcam, verifies the JPEG, then classifies it in mock
mode so no API quota is spent:

```bash
python -m tests.webcam_smoke              # camera index 0 (default webcam)
python -m tests.webcam_smoke --index 1    # a different camera
python -m tests.webcam_smoke --real-gemini  # real camera + REAL Gemini (needs GEMINI_API_KEY)
```

> **WSL2 note:** OpenCV inside WSL2 cannot see the Windows webcam — there is no
> `/dev/video*` unless you attach the USB device with `usbipd-win` and run a
> UVC-capable WSL kernel. The simplest path on Windows+WSL is to run the webcam
> test under **Windows Python** instead:
>
> ```powershell
> cd <repo>\sorter
> python -m venv .venv-win
> .venv-win\Scripts\activate
> pip install opencv-python python-dotenv google-genai
> python -m tests.webcam_smoke --real-gemini
> ```

**Seeing how Gemini categorizes an item** — `--real-gemini` above prints the raw
JSON verdict (`plastic`, `plastic_confidence`, `likely_contains_battery`,
`confidence`). `plastic` is `true` when `plastic_confidence` exceeds 0.5
(`config.PLASTIC_CONFIDENCE_THRESHOLD`). You can also
run the classifier standalone against the real API (`MOCK_HARDWARE=0`, key set):

```bash
python -m vision.classifier   # captures a frame and prints the classification
```

Both `vision/classifier.py` and `main.py` also log `Gemini classification: {...}`
/ `classified: {...}` at INFO to stdout and `logs/sorter.log`.

## Hardware bring-up order (on the real Pi)

Bring the system up incrementally, in this order, rather than plugging
everything in and running `main.py` cold:

1. **Verify power rails before connecting the Pi.** Confirm 3.3V and GND on
   the load cell and metal sensor headers with a multimeter before
   attaching anything to the Pi's GPIO pins.
2. **Bring up the Pi standalone — camera capture + one real Gemini call.**
   With `MOCK_HARDWARE=0` and a real `GEMINI_API_KEY` set:
   ```bash
   python -m vision.camera      # writes capture_test.jpg
   python -m vision.classifier  # captures a frame and classifies it
   ```
3. **`python -m sensors.load_cell` alone, calibrate zero/scale.** Run it
   standalone, tare with nothing on the scale, then place a known reference
   weight and adjust `HX711_REFERENCE_UNIT` / `HX711_ZERO_OFFSET` in
   `config.py` until `read_weight_g()` matches reality.
4. **`python -m sensors.metal_sensor` alone, confirm it toggles near
   metal.** Wave a metal object near the sensor face and confirm
   `metal_present` flips (LOW = detected, per the sensor's active-low
   wiring).
5. **Bring up the servo bus — see "Servo bring-up" below.** Loopback first
   (no servo attached), then scan, then a single move with the horn free,
   and only then `python -m actuators.servo_controller`.
6. **Run `logic/decision.py`'s `sort_side()` against printed sensor/vision
   output with servos disconnected.** Feed real metal + plastic readings from
   steps 2-4 into `sort_side(plastic, metal_present)` by hand (or via
   `python -m tests.test_decision`) and sanity-check which bin each combination
   picks before anything can physically move.
7. **Run `main.py` fully on the shared battery rail with a real test
   item.** Only after 1-6 pass, set `MOCK_HARDWARE=0` and run
   `python main.py` with power to the full system and a real item.

## Servo bring-up (direct UART, no adapter board)

The STS3215 talks half-duplex serial at 1 Mbps on a single data wire.
`actuators/sts_bus.py` speaks that protocol over the Pi's own UART and does
the job the USB adapter board used to do (framing, discarding its own echo,
parsing replies). No vendor SDK is involved.

### One-time Pi setup

On a Pi 5 the header UART (GPIO14/15) is off by default. Add to
`/boot/firmware/config.txt` and reboot:

```
dtparam=uart0=on
```

It appears as `/dev/ttyAMA0` (the default `SERVO_PORT`). Check with
`pinctrl get 14,15` - the pins should show `TXD0` / `RXD0`. If
`/boot/firmware/cmdline.txt` contains `console=ttyAMA0` or a
`serial-getty@ttyAMA0` service is running, remove/disable it so nothing else
writes to the servo bus. The user must be in the `dialout` group.

### Wiring

```
Pi GPIO14 (TX, pin 8)  --[ 1 kOhm ]--+
Pi GPIO15 (RX, pin 10) --------------+---- servo DATA
Pi GND (pin 6) --------------------------- servo GND  and  battery -
battery + -------------------------------- servo V+        (NEVER to the Pi)
```

The resistor is what makes one wire work both ways: the Pi drives the line
through it, and the servo can still pull the line when it replies. TX idling
high through the resistor is also the line's pull-up.

**Before connecting RX:** power the servo on its own and measure DATA to GND
with a multimeter. It must be 3.6 V or less. If it is near 5 V, put a voltage
divider or level shifter in front of GPIO15 - the Pi's pins are 3.3 V.

### Bring-up order

```bash
python -m actuators.sts_bus loopback      # 1. NO servo attached. Proves the UART runs at 1 Mbps and RX sees TX
python -m actuators.sts_bus scan --all    # 2. servo attached + powered: lists every ID that answers
python -m actuators.sts_bus watch 1       # 3. torque off; turn the horn by hand and read the ticks (pose recording)
python -m tools.nudge 1                   # 4. first motion: +100 ticks slowly at 30% torque, then back. Asks before moving;
                                          #    refuses if the servo's stored angle limits would turn it into a big swing
python -m actuators.sts_bus move 1 2048   #    then a full move to centre, horn free
python -m actuators.servo_controller      # 5. home -> safe -> flagged -> home (MOCK_HARDWARE=0)
```

The bus reports *which side* is broken instead of a generic timeout:

| Error | Meaning | Look at |
|---|---|---|
| `BusWiringError` | We sent bytes and heard nothing, not even our own echo | TX/RX tie, UART enabled, right `/dev/tty*` |
| `BusContentionError` | What we heard while sending wasn't what we sent | Short, second device on the line, bad ground |
| `ServoTimeout` | Echo was perfect, the servo said nothing | Servo power, the 3-pin cable, servo ID, baud rate |

Servo IDs come from `.env` (`SERVO_ID_SAFE_GATE`, `SERVO_ID_FLAGGED_GATE`).
Setting both to the same ID is the single-servo build: one servo tilts either
way from home. A USB bus adapter still works - set `SERVO_PORT=/dev/ttyACM0`;
the echo is auto-detected either way.

## Sorting rule

`logic/decision.py`'s `sort_side(plastic, metal_present)` decides which way the
bed tilts:

- **`plastic` AND no metal → the plastic bin** (`ServoPair.go_min()`). Only
  clean plastic goes here: Gemini must call it plastic *and* the inductive
  sensor must read no metal.
- **metal, or anything not called plastic → the other bin** (`go_max()`). Metal
  is a fast, high-precision veto.

The load cell gates the pipeline, so an item is only classified and sorted once
its weight is sensed — every weighed item goes to one of the two bins (there is
no "stay level" case; presence is already confirmed by weight). The bed rests at
`go_level()` between items. Gemini still reports `likely_contains_battery`, but it
is logged only and no longer influences sorting. The classifier fails toward
`plastic=False` on errors/timeouts, so an unreachable model treats the item as
non-plastic (→ the other bin). To flip which physical side is which, swap
`PLASTIC_BIN`/`OTHER_BIN` in `logic/decision.py`.

## Directory structure

```
sorter/
├── config.py                    # pins, calibration constants, thresholds, .env loading
├── sensors/
│   ├── load_cell.py              # HX711 wrapper
│   └── metal_sensor.py           # inductive sensor wrapper
├── vision/
│   ├── camera.py                 # OpenCV frame capture
│   └── classifier.py             # Gemini plastic + battery classification
├── actuators/
│   ├── sts_bus.py                # STS servo protocol over the Pi UART (half-duplex) + bring-up CLI
│   ├── servo_pair.py             # leader/follower bed pair: go_level / go_min / go_max (used by main.py)
│   └── servo_controller.py       # legacy home / move_safe / move_flagged demo (not used by main.py)
├── logic/
│   └── decision.py               # pure sort_side(plastic, metal) rule
├── tests/
│   ├── test_decision.py          # unit tests for the sorting rule
│   ├── test_sts_bus.py           # servo bus tests against a fake wire + fake servo
│   └── test_nudge.py             # nudge tool against a simulated moving servo
├── tools/
│   └── nudge.py                  # first-motion test: small, slow, gentle move and back
├── main.py                       # the state machine loop
├── .env.example
├── requirements.txt
└── README.md
```
