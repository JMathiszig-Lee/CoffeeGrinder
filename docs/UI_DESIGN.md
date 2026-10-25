# Grinder instrument display

The 320×240 Pico Display Pack 2.8 screen presents measured output RPM, the
selected target and load at the same time. The target starts at the saved
default, initially 110 RPM, and remains visible while the knob changes it.
Startup is OFFLINE: the application does not open CAN. The current executable
prototype provides an explicitly selected simulation for exercising controls.

## Layout and meaning

- The near-black face uses one engine-style instrument: a white boxed actual
  RPM readout with a smaller cyan `SET` value directly above it. The selected
  target remains visible during adjustment and running, within 5–110 RPM.
  The box, `SET` label, position and different marker shapes distinguish the
  two values as well as colour.
- The 0–120 output-RPM scale starts at the right and increases clockwise
  through 240 degrees. Labels appear every 20 RPM, with ticks every 10 RPM.
  A grey sector fills from zero to actual speed, ending at a white radial
  needle; a cyan triangular bug marks the selected target. The numerical
  readout, sector and needle use the same output-RPM scale. `RPM` remains
  visible beside the instrument.
- The horizontal `LOAD` bar and its numeric percentage are white normally;
  `RUNNING` status is green. At 80% or more the bar/percentage turn amber and
  the text adds `HIGH`. This fixed UI caution is separate from the configurable
  autostop and stall rules. Current demand includes mechanical friction and is
  not a calibrated torque reading. Simulation load is synthetic.
- Reserve amber for conditions needing attention and red for faults. Include
  an explicit status word or reason; colour alone never conveys the condition.
  Check contrast on the actual screen, especially coloured text on black.
- Keep a persistent operating-mode label at the top. With the CAN board
  removed, show `CAN OFFLINE`, measured RPM `---` and load `--`, retaining
  the editable target.
  Do not substitute zero for missing telemetry or show a stored IDLE current
  sample as live load. Missing feedback also hides the grey sector and actual
  needle; the selected target and its bug remain available.
- A demonstration that generates synthetic speed or load must retain a clear
  `SIMULATION` indication throughout. Hardware presence and motor running are
  separate states; a selected RPM does not prove either.
- Keep permanent information sparse: mode at y=12, compact load bar at y=204,
  and operating state at y=234. The y=220 message line is reserved for events
  and faults. There is no persistent help footer, title or separate target
  card.

Side legends sit next to their physical buttons, centred at y=60 and y=180:
`MENU` and `STOP` on the left; `DFLT` with the saved RPM and `SAVE` with
`HOLD 1s` on the right. The positions follow the
[Pimoroni dimensional drawing](https://cdn.shopify.com/s/files/1/0174/1800/files/PIM715-dimensional-drawing.png?v=1756824682):
button centres are 12.70/34.30 mm from the board edge, with the 43.20 mm active
LCD inset 1.90 mm. In landscape, `(12.70 - 1.90) / 43.20 * 240 = 60`, and the
other centre maps to 180. This associates each legend with its actual bezel
button rather than a generic row of controls.

Use the API's `load_valid` to gate the load display and its telemetry freshness
to gate actual RPM. Keep the selected target independent of feedback validity. Routine
UI work must stay short so input remains responsive. The retained DisplayIO
renderer updates changed labels, markers and bar geometry at up to 10 Hz;
the application loop sleeps about 1 ms between input/model updates. The
renderer and desktop previews share `Software/grinder_ui/panel.py` geometry.

## Controls and menu

| Input | Implemented action |
| --- | --- |
| Turn encoder | On the gauge, adjust the selected target by 1 RPM per count within 5–110. In a menu, navigate rows or change the current edit. |
| Press encoder | On the gauge, explicitly start when ready or stop while starting/running/stopping. In a menu, select a row or accept an edit. OFFLINE cannot start. |
| A — MENU / BACK | Open the menu. BACK cancels an unaccepted edit, then returns from a subpage or closes the root menu. |
| B | Stop. Holding B inhibits other control actions, including a simultaneous start. |
| X | On the gauge, recall the saved default. In a menu, select or accept, like pressing the encoder. |
| Hold Y for 1 second | On the gauge, save the selected RPM as the default. In a menu, persist and apply its accepted draft while stopped. A short press does not save. |

The root menu contains `DEMO`, `DEFAULT RPM`, `AUTOSTOP`, `STALL RECOVERY` and
`ACK FAULT`. Autostop exposes enabled, delay, idle load and load margin;
recovery exposes enabled, stall load, detect time, reverse RPM, reverse time
and maximum attempts. Turning while editing changes a draft; pressing accepts
the value. Accepted edits still show `SAVE TO APPLY` until a stopped hold-SAVE
succeeds. Opening or leaving a menu never applies settings or starts motion.

The demo submenu provides `RUN DEMO`, `EXIT DEMO`, `ADD BEANS`, `EMPTY HOPPER`
and `SIMULATE JAM`. RUN DEMO is an explicit start and returns to the gauge.
The default synthetic cycle waits for established forward speed, shows an
unloaded baseline, beans for six seconds, then an empty hopper. Load actions
exercise the automation without implying real CAN feedback. A simulated jam
clears after proven reverse motion; manual STOP cancels pending recovery.

## Automatic stopping and recovery

Autostop and automatic reverse recovery are both off by default. Autostop first
requires established speed and a qualified beans load above idle load plus
margin. Only then does continuous load at or below idle load start the delay.
Renewed load, insufficient speed or missing feedback resets the countdown.
Startup unloaded operation alone does not qualify a completed batch.

Recovery requires sustained high current demand together with near-stopped
shaft feedback. It uses fresh demand before display smoothing when available;
autostop uses smoothed load. It requests stop, waits for fresh confirmed stationary IDLE, reverses
at the configured speed, confirms reverse movement for the timed interval,
stops and confirms stationary IDLE again before resuming forward. Attempts are
bounded. Manual STOP cancels every pending restart. Feedback failures, failed
transitions or exhausted attempts latch a local fault; `ACK FAULT` requires a
fresh healthy confirmed stop and never clears a drive fault automatically.

Prototype settings and their permitted ranges are:

| Setting | Default | Range |
| --- | --- | --- |
| Autostop enabled | Off | Off / on |
| Delay | 3.0 s | 0.5–30 s |
| Idle load | 25% | 0–80% |
| Load margin | 8 points | 1–30 points; idle load plus margin ≤100% |
| Recovery enabled | Off | Off / on |
| Stall load | 80% | 50–100% |
| Detect time | 0.4 s | 0.1–3 s |
| Reverse RPM | 20 | 5–30 output RPM |
| Reverse time | 0.6 s | 0.1–2 s |
| Maximum attempts | 2 | 1–3 |

These current-demand thresholds require calibration with the assembled grinder.
Physical CAN integration and reversal remain unvalidated with this screen.

## Saved settings

The default RPM remains the legacy eight-byte `CGUI` record at NVM bytes 0–7;
an already saved 104 RPM remains valid. Bytes 8–27 contain a versioned `CGOP`
options extension with CRC16. Startup reads without writing. Blank options use
the defaults above; corrupt options are visibly reported with both automatic
features disabled. Foreign records, unknown extension versions and all other
NVM bytes are preserved. Explicit saves skip unchanged data and verify readback.
No settings file is written to CIRCUITPY and motor flash is not changed.

## Status light

The display's RGB LED provides a secondary status cue; the screen retains the
explicit mode, state and fault information. Fault red takes priority over all
other indications.

| Condition | LED indication |
| --- | --- |
| CAN OFFLINE | Amber |
| Live, idle / running | Dim white / green |
| Live, connecting, starting, stopping or missing RPM | Pulsing amber |
| Live load at or above 80% | Amber |
| Automatic unload countdown | Pulsing green in live mode / cyan in simulation |
| Stall recovery | Pulsing amber |
| Simulation, idle / running | Dim blue / cyan |
| Simulation transition | Pulsing cyan |
| Simulation load at or above 80% | Pulsing amber |
| Input or drive fault | Red |

Pulses have a two-second period. The global PWM brightness scale defaults to
8%, with additional dimming for idle colours. The common-anode channels use
GP26/GP27/GP28 for red/green/blue, with active-low PWM. The simulation colours
do not replace the persistent `SIMULATION` label.

The [UI package guide](../Software/grinder_ui/README.md) documents installation
and the current hardware boundary. The STEMMA QT encoder's Qw/ST wiring uses
GP4/GP5, which conflicts electrically with the original Pico-CAN-B allocation.
A mechanical spacer does not resolve that pin conflict. Shared SPI-controller
use also needs resolution and validation before combined operation; the
present UI does not demonstrate a working combined motor/display assembly.
The real API provides idle-only direction changes and a configurable stall
fallback exercised with a fake CAN peer; physical reverse motion has not been
validated. Blocking screen refresh also remains unsuitable for the live
controller's 100 ms servicing deadline. Backend injection is a software
integration point until wiring and display/control scheduling are validated.

## Human-factors reference

The user selected the FAA/Volpe report
[Human Factors Considerations in the Design and Evaluation of Flight Deck
Displays and Controls, Version 3.0](https://rosap.ntl.bts.gov/view/dot/87936),
September 2024, report DOT/FAA/AM-24/23. The following are paraphrased design
principles; page numbers are the report's printed numbers.

| Report location | Principle applied here |
| --- | --- |
| §2.1, p21 | Keep control-to-display response prompt. |
| §3.2, pp73, 76–78 | Use readable fonts, consistent labels and strong contrast. |
| §3.4, pp100–101 | Associate numerical and graphical indications, use directly useful units, and increase values clockwise/rightward. |
| §3.7, pp113–120 | Use warning/caution colours consistently and reinforce colour with text, position or shape. |
| §4.3.3, p172 | Clearly indicate the actual operating mode. |
| §4.4, p177 | Identify lost or invalid information without misleading frozen values. |

Direct [report PDF](https://rosap.ntl.bts.gov/view/dot/87936/dot_87936_DS1.pdf).
These principles inform a coffee-grinder UI; no aviation certification is
claimed. The layout, colours and dimensions above are this project's design
interpretation rather than a prescribed FAA grinder interface.

## Typography

Use official [B612](https://github.com/polarsys/b612) Regular for labels and
B612 Mono for stable numerical alignment. The renderer uses 12/14 pixel em
sizes for labels, 52 for actual RPM and 36 for selected RPM. A 52 pixel em
yields a 40 pixel numeric glyph box; position from metrics rather than filename size.
See [font provenance and reproducible conversion](../Software/assets/fonts/README.md).

Preserve the complete SIL Open Font License with font assets. Preload glyphs
before the live loop. Inspect the display at its normal viewing distance and
under the intended room lighting; pixel dimensions alone do not establish
legibility.
