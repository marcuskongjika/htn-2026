# Battery Sorter

Raspberry Pi 5 pipeline that flags waste items likely to contain a hidden
battery. A load cell (HX711) and an inductive metal sensor cross-check a
Gemini photo classification of the item's material, then drive STS3215
smart servos to tilt the item into a "safe" or "flagged" bin. The servos are
driven straight from the Pi's UART pins - there is no bus adapter board.

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
5. **Bring up the servo bus — see "Servo bring-up" below.** Loopback first
   (no servo attached), then scan, then a single move with the horn free,
   and only then `python -m actuators.servo_controller`.
6. **Run `logic/decision.py`'s fusion function against printed
   sensor/vision output with servos disconnected.** Feed real
   weight/metal/material readings from steps 2-4 into `fuse()` by hand (or
   via `python -m tests.test_decision`) and sanity-check the verdicts
   before anything can physically move.
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
│   ├── sts_bus.py                # STS servo protocol over the Pi UART (half-duplex) + bring-up CLI
│   └── servo_controller.py       # home / move_safe / move_flagged on top of sts_bus
├── logic/
│   └── decision.py               # pure fusion function
├── tests/
│   ├── test_decision.py          # unit tests for the fusion rule
│   ├── test_sts_bus.py           # servo bus tests against a fake wire + fake servo
│   └── test_nudge.py             # nudge tool against a simulated moving servo
├── tools/
│   └── nudge.py                  # first-motion test: small, slow, gentle move and back
├── main.py                       # the state machine loop
├── .env.example
├── requirements.txt
└── README.md
```
