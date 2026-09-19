# Battery Sorter

Raspberry Pi 5 pipeline that flags waste items likely to contain a hidden
battery. A load cell (HX711) and an inductive metal sensor cross-check a
Gemini photo classification of the item's material, then drive two STS3215
smart servos to tilt the item into a "safe" or "flagged" bin.

The pipeline is an explicit state machine (`main.py`):

```
IDLE -> MEASURING -> CLASSIFYING -> DECIDING -> ACTUATING -> RESETTING -> IDLE
```

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
laptop:

```bash
python main.py
```

This runs one full cycle (the mock load cell's baseline weight sits above
`WEIGHT_TRIGGER_G`, so `IDLE` triggers immediately) and logs every state
transition and sensor/decision value to stdout and to `logs/sorter.log`
(rotating, see `config.LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`).

To test just the material classifier against the real Gemini API while
everything else stays mocked, set `MOCK_HARDWARE=0` but note that will also
try to open the camera/GPIO/serial — see the bring-up order below for how
to exercise modules individually instead.

Run the decision-logic unit tests any time (pure function, no mocking
needed):

```bash
python -m pytest tests/test_decision.py -v
```

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
5. **Bring up both servos over USB one at a time, confirm IDs 1 and 2
   respond independently.** Run `python -m actuators.servo_controller`
   with only one servo connected at a time first, watching for the correct
   ID responding, before connecting both.
6. **Run `logic/decision.py`'s fusion function against printed
   sensor/vision output with servos disconnected.** Feed real
   weight/metal/material readings from steps 2-4 into `fuse()` by hand (or
   via `python -m tests.test_decision`) and sanity-check the verdicts
   before anything can physically move.
7. **Run `main.py` fully on the shared battery rail with a real test
   item.** Only after 1-6 pass, set `MOCK_HARDWARE=0` and run
   `python main.py` with power to the full system and a real item.

## Fusion rule

`logic/decision.py`'s `fuse()` flags an item if the metal sensor detects
metal **or** Gemini's classification says `likely_contains_battery`,
whichever fires first — weight is captured and used to trigger the
`IDLE -> MEASURING` transition but does not currently affect the verdict
(reserved for future tuning). See the docstring in that file for the full
rationale, including why the classifier fails toward "flagged" rather than
"safe" on errors/timeouts.

## Directory structure

```
sorter/
├── config.py                    # pins, calibration constants, thresholds, .env loading
├── sensors/
│   ├── load_cell.py              # HX711 wrapper
│   └── metal_sensor.py           # inductive sensor wrapper
├── vision/
│   ├── camera.py                 # OpenCV frame capture
│   └── classifier.py             # Gemini material classification
├── actuators/
│   └── servo_controller.py       # STS3215 servo wrapper
├── logic/
│   └── decision.py               # pure fusion function
├── tests/
│   └── test_decision.py          # unit tests for the fusion rule
├── main.py                       # the state machine loop
├── .env.example
├── requirements.txt
└── README.md
```
