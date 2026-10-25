# Commissioning tools and hardware record

These advanced diagnostics and bounded motor profiles are explicitly invoked.
They are separate from the normal UI installer, which never runs motor tests.
See the [project README](../../README.md) for installation, the
[UI guide](../grinder_ui/README.md) for operation and wiring, and the
[developer reference](../README.md) for the reusable motor API.

The measurements below were recorded on **25 October 2026**, using a Pico 2
with CircuitPython 9.2.8, a Waveshare Pico-CAN-B and a SteadyWin GIM6010-8.
Motion/speed checks used a nominal **24 V** supply and a mechanically unloaded
motor. A separate secured brake/friction fixture supplied the load-current
check. These results do not establish assembled-grinder behavior, physical
reversal or combined display/encoder/CAN operation.

## Programs and prerequisites

| Program | Purpose |
| --- | --- |
| [code.py](code.py) | Fixed 15-second listen-only SPI/register/CAN diagnostic; contains no motor commands. |
| [read_identity.py](read_identity.py) | Seven fixed read-only identity requests, gated by healthy IDLE feedback; restores listen-only mode. |
| [motor_test.py](motor_test.py) | Configuration inspection or a one-shot three-second 10 RPM trial, with watchdog proofs. |
| [conical_sweep.py](conical_sweep.py) | Bounded unloaded 20/50/80/110 output-RPM sweep. |
| [current_sense.py](current_sense.py) | Low-speed signed-current logging with a secured load fixture. |
| [run_on_pico.py](run_on_pico.py) | Host raw-REPL runner for inspection and motion profiles, with captures/CSV output. |

Current UI startup is **CAN OFFLINE**. Historical references to passive
`code.py` describe the diagnostic setup used here, not the current UI entry
point. The commissioning runner deliberately requires this directory's
[code.py](code.py) as `CIRCUITPY/code.py` and refuses another startup file. It
uploads a one-shot profile through the REPL without replacing startup. Preserve
the existing application before deliberately selecting that diagnostic setup;
do not remove the guard to run over the UI.

The host runner requires `pyserial`, compatible `adafruit_mcp2515` and
`adafruit_bus_device` libraries on the Pico, and explicit serial-device,
mounted-volume and board-UID arguments. It checks the Pico 2 identity/UID and
exact passive startup file before accessing the REPL. Replace these host/board
placeholders before use. `PICO_UID` is the exact value after `UID:` in the
target volume's `boot_out.txt`; the separate platform Board ID must be
`raspberry_pi_pico2`.

```sh
PICO_PORT=/path/to/serial-device
PICO_VOLUME=/path/to/CIRCUITPY
PICO_UID=YOUR_PICO_UID

# Configuration inspection; no motion requested.
python Software/bringup/run_on_pico.py --port "$PICO_PORT" --volume "$PICO_VOLUME" --board-id "$PICO_UID" --action inspect

# Explicit fixed motion profiles; keep the motor mechanically unloaded.
python Software/bringup/run_on_pico.py --port "$PICO_PORT" --volume "$PICO_VOLUME" --board-id "$PICO_UID" --action move
python Software/bringup/run_on_pico.py --port "$PICO_PORT" --volume "$PICO_VOLUME" --board-id "$PICO_UID" --action sweep110

# Only with motor/fixture secured, initial brake released and hands clear.
python Software/bringup/run_on_pico.py --port "$PICO_PORT" --volume "$PICO_VOLUME" --board-id "$PICO_UID" --action fixture_load --fixture-ready
```

Run from the repository root. The fixture action refuses board access without
its readiness flag. Host Ctrl-C forwards an interrupt to the Pico and continues
reading while device cleanup attempts to confirm IDLE. Sweep/fixture runner
deadlines are 90/120 seconds; profiles have shorter motion bounds. There are
no automatic retries or current increases. Stop confirmation comes from fresh
telemetry, not from the host program exiting.

## Wiring and protocol identity

| Pico GPIO | Waveshare Pico-CAN-B function |
| --- | --- |
| GP4 | MISO |
| GP5 | Chip select |
| GP6 | SPI clock |
| GP7 | MOSI |

The [Waveshare schematic](https://files.waveshare.com/upload/1/13/PICO-CAN-B-sch.pdf)
defines this mapping. The 16 MHz MCP2515 runs at **500 kbit/s**. GP4/GP5
conflict with the UI's Qw/ST encoder, and the original CAN/LCD banks both
select SPI0. See the UI guide before planning combined wiring.

The first passive capture verified reset CANSTAT/CANCTRL `80/87`, listen-only
`60/67` and CNF1/CNF2/CNF3 `00/F0/86`. It received **1,651 standard frames in
15 seconds**, with no receive-overflow flags. IDs `0x21` and `0x29` were
subsequently confirmed as node-1 heartbeat and encoder estimates. Listen-only
mode sends no acknowledgements; silence alone does not prove a wiring failure.

The subsequent read-only check observed:

| Field | Value |
| --- | --- |
| Hardware version / variant | **3.10 / 1** |
| Firmware | **0.5.14**, `fw_version_unreleased=1` |
| CAN Simple node | **1** |
| Encoder position / speed at identity read | **0 rotor rev / 0 rotor rev/s** |

The supplied general GIM driver specification, with commands such as
`0x91`/`0x94` in payload byte zero, describes a different protocol family.
The GDS68 CAN Simple manual and exact firmware endpoint table match this
motor's observed traffic. The motor name alone does not identify its protocol.

Technical sources used for commissioning:

- [SteadyWin downloads](https://steadywin.cn/en/col.jsp?id=124) separates GDS68
  documentation from GDS34/GDS6/GDS810 specifications.
- [GIM6010-8 GDS68 manual, revision 2.2](https://download.s21i.co99.net/14180476/0/0/ABUIABA9GAAghZu6wQYo7L6lVw.pdf?f=SteadyWin+GIM6010-8+Motor+Manual_rev2.2.pdf&v=1747881349),
  printed page 48, defines rotor-based encoder units.
- [Manufacturer SDK `MWMotor.c`](https://gitee.com/cyberbeast/mwmotorsdk/blob/master/MWSDK/MWMotor.c)
  uses command `0x004` for read request/reply and `0x005` for writes. Node-1
  IDs are `0x024`/`0x025`; reads use opcode zero and eight bytes.
- [Firmware 0.5.14 endpoints](https://bl.cyberbeast.cn/actuator/endpoints_0.5.14.json)
  defines hardware fields 6–8, firmware fields 9–12, watchdog timeout 150 and
  enable 151. Version fields are read-only uint8; reply byte 4 carries each
  value. Endpoint maps and reply layouts differ across firmware revisions.

Encoder velocity is **rotor revolutions per second**. For the commissioned
8:1 gearbox:

```text
output_rpm = rotor_rev_s * 60 / 8
target_rotor_rev_s = target_output_rpm * 8 / 60
```

## First motion and watchdog proofs

The motor completed a **three-second 10 output-RPM target** trial at **23.90 V**.
Encoder travel was **0.4668 output revolutions**. It used a **1 A** configured
current cap and **2 rotor rev/s** speed cap (15 output RPM), enabled velocity
limiting/overspeed protection and verified readback. Initial RAM settings had
been 30 A / 30 rotor rev/s with both protections disabled.

Before positive velocity, zero-velocity CAN traffic kept the enabled **0.5 s
drive watchdog** healthy. With addressed commands withheld, IDLE heartbeat
reported watchdog error `0x800` after **0.581 s**. Repeating the silence test in
closed loop at zero target disarmed the drive to IDLE with the same expected
error. Only those deliberately induced watchdog errors were cleared.

The trial fed the watchdog every 50 ms and checked fresh heartbeat/encoder
feedback. It ended with confirmed stationary IDLE and a final measured speed
of **-0.229 output RPM**, inside the stop threshold. Original disabled/zero
watchdog settings were restored only after confirmation. No calibration or
flash-save was sent. Profile-specific restoration differs from the reusable
API lifecycle documented in the developer reference.

An initial attempt timed out on a configuration reply before activation and
confirmed IDLE during cleanup. Bounded receive servicing around transmit
bursts resolved missed replies. One-shot transmit mode, serialized completion
checks and pending-frame aborts prevent stale motion frames remaining queued
for replay. Watchdog command silence was tested; physical cable removal and
loaded-grinder stopping were not.

## Unloaded 71 mm conical speed sweep

The intended burrs were identified as **71 mm conical burrs from a KafaTek
Monolith**. KafaTek's [MC4 Classic specification](https://www.kafatek.com/index.php/store/mc4-classic-grinder/)
lists 71 mm Mazzer 186C burrs at **110 RPM**, also identified in its
[replacement-burr listing](https://www.kafatek.com/index.php/store/71mm-conical-burrs-for-mc3/).
This supplied an initial speed benchmark rather than a loaded-grinder rating.

At 24 V and a **1 A configured cap**, the sweep ramped up at 25 RPM/s through
20, 50, 80 and 110 output RPM, then down at 20 RPM/s. The drive speed cap was
temporarily 130 output RPM. Each stage checked its final second of fresh
samples: mean velocity and position-derived RPM within 5% or 1 RPM, and at
least 90% of samples within 10% or 3 RPM.

| Output target | Mean encoder RPM | Position-derived RPM | Sample range | Fresh samples |
| --- | ---: | ---: | ---: | ---: |
| 20 RPM | 20.049 | 20.016 | 18.368–21.458 | 101 |
| 50 RPM | 50.048 | 50.110 | 48.466–51.498 | 101 |
| 80 RPM | 80.013 | 80.172 | 76.790–82.455 | 99 |
| 110 RPM | 109.995 | 111.361 | 106.144–113.754 | 100 |

All stages passed. The highest hold lasted three seconds after a half-second
settling interval. The profile covered **21.873 output revolutions**. Maximum
reported bus voltage was **23.980 V**. Reported Iq briefly reached **1.362 A
absolute** without reaching the sustained-current abort condition; the
configured cap was not an instantaneous measured-current ceiling.

The sweep repeated the zero-target watchdog proofs and confirmed stationary
IDLE before restoring the earlier 2 rotor rev/s cap, disabled Iq/bus periodic
streams and original watchdog settings. It returned CAN to listen-only mode
without flash-save. This verifies unloaded speed tracking for that setup,
not grinding torque or bean-load behavior.

## Secured fixture current response

The fixture profile uses **10 output RPM** and a **0.5 A configured cap**.
The motor body and brake/friction fixture were secured, initial brake load
released and hands kept clear. A handheld shaft/printed connector was not
the test fixture.

It samples signed demanded/measured Q-axis current at 50 Hz, produces 200 ms
current estimates and exports 10 Hz snapshots with speed and bus measurements.
The bounded sequence is a two-second ramp, 2.1-second unloaded baseline,
up to 60 seconds for applied load, then two-second ramp-down. Motion is limited
to 75 seconds / 15 output revolutions. Stable baseline speed and current
headroom are required. Once armed, speed below 3 RPM for 200 ms requests
IDLE/coasting without reverse, retry or a current increase.

The first two runs recorded unloaded current because load was not applied in
their shorter windows; they do not establish a loaded response. The later
secured-fixture run measured:

| Metric | Unloaded baseline, 2.0–4.1 s | Final loaded second, 10.5–11.5 s |
| --- | ---: | ---: |
| Mean demanded Iq | -0.179 A | -0.465 A |
| Mean filtered measured Iq | -0.175 A | -0.495 A |

These are means of the logged 10 Hz snapshots; filtering used faster onboard
samples. Demanded current reached **-0.500 A** and the last logged motion
sample fell to **0.687 output RPM**, triggering the sustained-low-speed stop.
Fresh telemetry confirmed stationary IDLE and original periodic/watchdog
settings were restored. The 0.5 A cap remained in RAM; no flash-save was sent.

`TEST STOP: ... Sustained low speed` was the intended protective outcome,
although the host runner reports a stopped trial as non-completion. Increased
magnitude of signed averages shows a qualitative load response, not resistance
in ohms or calibrated shaft torque. Transient current peaks exceeded the cap,
and RX0 overflow (`MCP2515 EFLG=0x40`) occurred. Precision calibration needs
improved logging and further validation.

## Local artifacts and host validation

Board backups, vendor files and captures are kept under ignored `.local/`.
These are historical local artifacts, not required installation files.

| Check | Artifact names under `.local/` |
| --- | --- |
| Passive / identity | `can-passive-500k.log`, `can-identity.log` |
| First motion | `motor-move.log`, `motor-move-attempt1.log` |
| Sweep | `motor-sweep110.log`, `.csv`, `-stages.csv`, `-summary.json` |
| Fixture | `motor-fixture_load.log`, `.csv`, `-loaded.log`, `-loaded.csv`, `-summary.json`; early load-window captures have `-attempt1`/`-attempt2` suffixes. |
| Reusable API IDLE check | `api-idle-check.log`, separate from these motion profiles. |

The reusable API subsequently completed a ten-second IDLE-only hardware check
with zero steady receive overflows and confirmed asynchronous shutdown. Its
active motion/reversal policies and the current offline UI are distinct from
these historical profiles.

Run commissioning host checks from the repository root:

```sh
python -m unittest discover -s Software/bringup/tests -q
```

They exercise passive capture, identity gating, watchdog proofs, limits and
readback, stale feedback, staged tracking, fixture startup/stall stops and
host stop relay. See the developer reference for the separate API/UI suite.
