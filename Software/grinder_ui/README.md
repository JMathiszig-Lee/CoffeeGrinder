# Pico grinder instrument panel

This is the native **CircuitPython 9.2.8** screen/encoder application for the
Pico 2, Pimoroni **Pico Display Pack 2.8, 320×240**, and a STEMMA QT seesaw
rotary encoder connected through the display's Qw/ST socket. It uses retained
`displayio` graphics and the bundled B612 fonts; no firmware replacement or
MicroPython/PicoGraphics installation is needed.

For normal setup, use the [project installer and quick start](../../README.md#install).
The manual-copy instructions below are for development.

The application starts **CAN OFFLINE** and never opens CAN. The CAN board is
currently removed. The software offers a labelled simulation for trying the
controls; it does not imply a connected motor or a working combined hardware
stack.

## What is on the screen

The engine-style instrument has one large **white boxed actual-RPM** readout
with a smaller cyan **SET** value directly above it. Its 0–120 **output-RPM**
scale starts at the right and increases clockwise through 240 degrees. A grey
sector fills from zero to actual speed and ends at a white radial needle;
a cyan triangular bug marks the selected target. Scale labels appear every
20 RPM, with ticks every 10 RPM. The `RPM` unit remains visible beside it.

OFFLINE shows actual RPM `---` and load `-- %`, rather than inventing zero
feedback. In demo, the top strip always reads **SIMULATION**, including when
stopped. Starting the demo animates synthetic actual RPM and load. Faults
have a red status/reason; invalid feedback hides the actual needle and sector
as well as showing dashes for the readout. The selected target stays visible.

The compact `LOAD` bar represents current demand as a percentage, not calibrated
shaft torque or resistance. Normal load is white. At **80% or higher**, amber
`HIGH` text and an amber bar provide a provisional UI caution. This fixed
display threshold is separate from the configurable automation rules below.
Real current demand also includes gearbox/bearing friction; demo load is
entirely synthetic.

The top line always identifies the mode; the bottom line shows operating
state. The message line is used only for events or faults. There is no
persistent help footer or separate target/default card. Softkey legends sit
beside the actual buttons: A/B at the left, X/Y at the right, centred at
screen y=60 and y=180. These positions follow
[Pimoroni's dimensional drawing](https://cdn.shopify.com/s/files/1/0174/1800/files/PIM715-dimensional-drawing.png?v=1756824682).

See the [desktop preview](../previews/instrument_preview.png) and
[design rationale and FAA references](../../docs/UI_DESIGN.md).

## Controls

| Control | Action |
| --- | --- |
| Turn encoder | On the gauge, change target by 1 RPM per count within 5–110 RPM. In menus, navigate rows or change the current edit. |
| Press encoder | On the gauge, explicitly start a ready backend or stop while starting/running/stopping. In menus, select or accept an edit. OFFLINE cannot start. |
| A — MENU / BACK | Open the menu. BACK first cancels an unaccepted edit, then returns from a subpage or exits the root menu. |
| B — STOP | Stop. While held, B inhibits the other controls, so stop takes priority over a simultaneous start. |
| X | On the gauge, recall the saved default shown beneath DFLT. In menus, select or accept, like pressing the encoder. |
| Y — SAVE | **Hold for one second**. On the gauge, save the selected RPM as the default. In menus, persist and apply accepted draft settings while stopped. A short press does not save. |

Buttons use 25 ms debounce; STOP cancels automation on its first observed
pressed level. An encoder position reset or I2C failure latches
an input fault and requests a stop; restore the input connection and restart
the application. Startup and opening a menu never start the motor.

## Menu and automatic features

The root menu contains **DEMO**, **DEFAULT RPM**, **AUTOSTOP**, **STALL
RECOVERY** and **ACK FAULT**. Turn to choose a row; press the encoder or X to
open it. In an edit, turning changes a draft and pressing accepts the value.
A cancels the current unaccepted edit. Accepted menu changes remain a draft
marked **SAVE TO APPLY** until hold-Y succeeds while stopped; leaving a page
does not apply them. The gauge's existing hold-Y default-RPM save is unchanged.

Autostop exposes **ENABLED**, **DELAY**, **IDLE LOAD** and **LOAD MARGIN**.
Recovery exposes **ENABLED**, **STALL LOAD**, **DETECT TIME**, **REVERSE RPM**,
**REVERSE TIME** and **MAX ATTEMPTS**.

| Option | Default | Allowed range |
| --- | --- | --- |
| Autostop | Off | Off / on |
| Delay | 3.0 s | 0.5–30 s |
| Idle load | 25% | 0–80% |
| Load margin | 8 percentage points | 1–30; idle load + margin ≤100% |
| Automatic recovery | Off | Off / on |
| Stall load | 80% | 50–100% |
| Detect time | 0.4 s | 0.1–3 s |
| Reverse speed | 20 output RPM | 5–30 output RPM |
| Reverse time | 0.6 s | 0.1–2 s |
| Maximum attempts | 2 | 1–3 |

Both automatic features are **off by default**. With autostop enabled, the
engine first qualifies established speed and load above idle load plus margin.
After beans have been observed, load continuously at or below idle load starts
the delay countdown. Renewed load, insufficient speed or missing feedback
resets the countdown. Starting with an empty hopper alone cannot qualify a
completed batch.

Stall detection combines sustained high current demand with near-stopped shaft
feedback. It uses fresh demand before display smoothing when available, while
autostop uses the smoothed load readout.
If recovery is enabled, it stops and waits for fresh confirmed stationary
IDLE, reverses at the configured RPM, proves reverse movement for the timed
interval, stops and confirms stationary IDLE again, then resumes forward.
Recovery has a bounded attempt count. **B STOP cancels every pending restart.**
Missing feedback, failed transitions or exhausted retries stop and latch a
local fault. **ACK FAULT** clears only a local automation fault after fresh
healthy stopped feedback; it never automatically clears a drive fault.
Disabling recovery prevents automatic reversal; it does not remove stall
protection.

The **DEMO / TEST** submenu offers **RUN DEMO**, **EXIT DEMO**, **ADD BEANS**,
**EMPTY HOPPER** and **SIMULATE JAM**. RUN DEMO explicitly starts and returns
to the gauge. The default cycle gives an unloaded baseline after established
speed, six seconds of beans load, then empty load. The other actions exercise
load qualification and jam recovery; the simulated jam clears only after
reverse motion is proved. The persistent SIMULATION label remains visible.

These are prototype current-demand thresholds. Calibration with beans and
the assembled grinder, physical CAN integration and actual reversal are still
pending; synthetic demo success does not establish those behaviors on hardware.

## Onboard status LED

The Pico Display Pack's RGB LED supplements the labelled screen state:

| State | LED |
| --- | --- |
| CAN OFFLINE | Amber |
| Live idle / running | Dim white / green |
| Live transition or missing RPM | Pulsing amber |
| Live load at or above 80% | Amber |
| Automatic unload countdown | Pulsing green in live mode / cyan in simulation |
| Stall recovery | Pulsing amber |
| Simulation idle / running | Dim blue / cyan |
| Simulation transition | Pulsing cyan |
| Simulation load at or above 80% | Pulsing amber |
| Input or drive fault | Red, with highest priority |

Pulses have a two-second period. The global brightness scale defaults to 8%;
idle colours are dimmer. Active-low PWM drives red GP26, green GP27 and blue
GP28. Closing the application turns all channels off before releasing them.
The encoder's separate NeoPixel is not used.

## Persistent settings

The initial default is **110 RPM**; an existing saved RPM is preserved.
Default RPM retains bytes 0–7 of
`microcontroller.nvm`: `CGUI`, a little-endian 16-bit RPM value and its 16-bit
complement. A versioned **CGOP** extension at bytes **8–27** stores the menu
options with a CRC16 checksum; timing values use millisecond resolution.
Startup never writes. Blank options use the defaults above, while corruption
is visibly reported with automatic features safely disabled. A blank/corrupt
RPM record falls back to 110 RPM.

Foreign RPM records, unknown/foreign extension records and unrelated bytes are
preserved. A foreign extension prevents option saving but does not prevent a
valid legacy RPM save. Explicit saves skip unchanged data and verify readback;
failures are reported without claiming persistence. Nothing writes a settings
file to CIRCUITPY or changes motor flash.

## Install or run manually

Use CircuitPython libraries from a bundle compatible with 9.x. The application
imports `adafruit_st7789`, `adafruit_display_text`, `adafruit_bitmap_font`,
`adafruit_seesaw` and their bundle dependencies, including
`adafruit_bus_device`. DisplayIO, FourWire, VectorIO and PWMIO are firmware
modules.

1. Back up the existing startup program before choosing to replace it.
2. Copy this `grinder_ui` directory to `CIRCUITPY/lib/grinder_ui`.
3. Copy `Software/assets/fonts/` to `CIRCUITPY/fonts/`, preserving its `OFL.txt`.
4. Keep/install the matching Adafruit dependencies in `CIRCUITPY/lib/`.
5. For automatic UI startup, copy `Software/display_code.py` to
   `CIRCUITPY/code.py`. Its startup mode is OFFLINE.

To try the application without changing `code.py`, interrupt the current
program at the USB REPL after copying the package/fonts, then run:

```python
from grinder_ui.app import main
main()
```

For a bounded, motor-free check:

```python
main(run_seconds=10)
```

The bounded call returns a captured view and label bounds for checking layout.
The normal application refreshes changed screen content at up to 10 Hz and
services inputs/model between frames with an approximately 1 ms loop delay.
Display transfer and input transaction time still contribute to latency.
The simulation tolerates redraw gaps up to 750 ms; live automation retains
its tighter sampling guard and the motor controller's separate 100 ms deadline.

## Wiring and future motor integration

| Pico GPIO | Current use |
| --- | --- |
| GP4 / GP5 | Qw/ST I2C SDA / SCL for the encoder |
| GP12 / GP13 / GP14 / GP15 | Display buttons A / B / X / Y |
| GP16 / GP17 | LCD data-command / chip select |
| GP18 / GP19 | LCD SPI clock / MOSI |
| GP20 | LCD backlight |
| GP26 / GP27 / GP28 | Onboard RGB LED red / green / blue, active-low PWM |

The encoder factory expects seesaw address **0x36** and product firmware ID
**4991**. It checks that identity and enables the button pull-up before use.
Its direction is configured for clockwise rotation to increase the setpoint;
`open_encoder(invert=True)` supports an encoder mounted with the reverse sense.
Position/button register reads are scheduled cooperatively across the seesaw
response interval, allowing a full 8 ms after register selection finishes.
A delayed screen redraw does not itself indicate an encoder failure. Repeated
I2C lock contention lasting over 250 ms, or an actual I2C read/write error,
still latches an input fault and requests a stop. The encoder NeoPixel is left
untouched.

The original Pico-CAN-B uses GP4 as MISO and GP5 as chip select. Those signals
conflict with the Qw/ST I2C pins. **A spacer changes clearance, not pin routing.**
The display on GP18/GP19 and CAN on GP6/GP7 also both select SPI0;
CircuitPython cannot create separate hardware SPI buses on those pin banks
at the same time. One possible redesign is to isolate the CAN board's original
signal connections and route its MISO/CS/SCK/MOSI to GP8/GP9/GP10/GP11 on SPI1.
That requires physical wiring changes and a configurable CAN adapter before
validation. Do not treat the existing adapters as a tested simultaneous
configuration. See [CircuitPython's SPI implementation](https://github.com/adafruit/circuitpython/blob/9.2.8/ports/raspberrypi/common-hal/busio/SPI.c)
and the [Pimoroni schematic](https://cdn.shopify.com/s/files/1/0174/1800/files/pico_display_2_8_schematic.pdf?v=1728314048).

`UiModel` accepts an injected backend implementing `set_rpm`, `start`, `stop`,
`update`, `snapshot` and `close`. That is a software integration point for the
motor API. The default application uses `OfflineMotor`; MENU → DEMO → RUN DEMO
explicitly selects and starts `DemoMotor`. The real API supports idle-only
direction changes and configurable stall fallback, exercised with a fake CAN
peer; physical reversal has not been validated. Live backend injection is not
a claim that the present display, encoder and CAN wiring can operate together.
Blocking display refresh is also still unsuitable for the live controller's
100 ms servicing deadline. Combined use needs validated wiring and control/
display scheduling before this software integration point can drive a motor.

## Validation — 25 October 2026

The earlier screen/encoder layout completed an actual Pico check with B612
text, offline readings, simulated RPM/load changes and a separate target
change. Its bounded control-loop check reported `encoder=OK`, and the CAN
driver was not loaded. Encoder direction was corrected following the user's
on-device feedback.

The current MFD layout and RGB LED changes completed an actual Pico check:
offline, demo idle/starting/running, synthetic high load and an input fault.
All nonempty label rectangles fit within 320×240, the PWM readbacks matched
the state colours, and the CAN driver remained unloaded. The complete control
loop also reported `encoder=OK`. That initial check covered only a short demo
run and missed a redraw-induced input timeout during the ramp to 110 RPM.

The timeout was reproduced: a 259 ms gauge redraw exceeded the old 250 ms
input-age guard before it attempted a fresh I2C read. The guard now measures
failed lock acquisition instead, and the fan uses fewer, fixed arc vertices
to reduce redraw work. A subsequent 35-second on-device demo reached 110 RPM,
changed targets, stopped and restarted, and recovered from an intentional
400 ms scheduling pause without an encoder fault. CAN remained unloaded.
Those on-device checks predate the settings menu and automatic features.
Their physical integration and reversal validation remain pending.

The menu and automation then passed a 45-second Pico display simulation:
autostop after a loaded batch, signed reverse to -20 RPM, forward resume to
110 RPM, browsing settings during recovery, and STOP cancellation of a second
recovery. No encoder faults occurred and all label bounds fit the screen.
Menu switching was reduced from over one second to roughly 0.4 seconds;
ordinary page updates took about 0.12–0.15 seconds. A separate check verified
the option record in actual NVM, preserving the current saved RPM and every
byte outside the extension. CAN stayed unloaded throughout these checks.
Host suites cover the UI, automation, controller, installer and commissioning
tools; see the [developer guide](../README.md#installation-and-validation).

Run the host suites from the repository root:

```sh
python -m unittest discover -s Software/tests -q
python -m unittest discover -s Software/bringup/tests -q
```

The UI is the offline startup application. The commissioning runner requires
its own passive startup before use; see the [commissioning guide](../bringup/README.md).

B612 is distributed under SIL Open Font License 1.1; see
[font provenance and conversion](../assets/fonts/README.md). The FAA report
informs the interface's human-factors choices; no aviation certification is
claimed.
