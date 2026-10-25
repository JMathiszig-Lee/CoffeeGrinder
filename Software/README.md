# Software developer reference

See the [project README](../README.md) for installation, the [UI
guide](grinder_ui/README.md) for controls/settings/wiring, and [commissioning and
bring-up](bringup/README.md) for advanced diagnostics and historical motor measurements.

The current CircuitPython application starts **CAN OFFLINE**. It runs the display,
encoder, settings menu and explicitly selected simulation without opening CAN. The motor
API and automation supervisor are implemented, but their combination with the physical
display/encoder/CAN stack is not validated. Installing the UI never runs a motor test or
requests motion.

## Packages and entry points

| Path | Purpose |
| --- | --- |
| [display_code.py](display_code.py) | Offline UI startup entry point. |
| [grinder_ui/](grinder_ui/) | Input handling, instrument/menu rendering, saved settings and simulated motor. |
| [grinder_ui/automation.py](grinder_ui/automation.py) | Optional observed-load autostop and bounded stop/reverse/resume supervisor. |
| [grinder_api/](grinder_api/) | Cooperative motor controller, firmware-specific protocol and CircuitPython CAN transport. |
| [examples/ui_loop.py](examples/ui_loop.py) | API loop with input callbacks; its default adapter produces no start events. |
| [examples/verify_idle.py](examples/verify_idle.py) | Explicit, bounded API connection check which never calls `start()`. |
| [bringup/](bringup/) | Advanced diagnostics and motion profiles, separate from normal installation. |
| [tools/](tools/) | Host installation, font and preview tools. |

The motor API runs in the Pico's CircuitPython process. Host Python supplies
installation, tests and commissioning helpers. Importing `grinder_api` or constructing
`GrinderController` does not initialize hardware; the explicit `open_transport()`
factory opens CAN.

## Motor API lifecycle

The current adapter targets the commissioned SteadyWin configuration: CAN Simple node 1,
firmware **0.5.14**, an **8:1** gearbox and a nominal **24 V** supply. `connect()`
checks firmware before configuration writes and requires fresh, healthy stationary IDLE.
Setup does not start the motor.

```python
import time
from grinder_api import GrinderController
from grinder_api.circuitpython import open_transport

# Advanced motor-only setup; use validated CAN wiring, not the current UI stack.
motor = GrinderController(open_transport(), node_id=1, max_rpm=110,
                          current_limit_a=0.5, gear_ratio=8, clock=time)
motor.connect()

def on_explicit_start():
    if motor.snapshot()["ready"]:
        motor.set_rpm(110)
        motor.start()

def on_stop():
    motor.stop()

def service_once():
    motor.update()
    status = motor.snapshot()
    # Display measured_rpm only with valid telemetry; None means unavailable.
    # Display load_percent only when load_valid is true.
    return status
```

Call `update()` every **1–5 ms**, including while connecting, IDLE, faulted, stopping
and closing. A gap over **100 ms** during starting/running latches a fault before
buffered feedback can be treated as fresh. Reducing display refresh frequency does not
make a long blocking redraw compatible with this deadline. The current UI's blocking
screen updates require a scheduling redesign before live integration.

The normal service path contains no sleeps. Exceptional SPI abort/mode cleanup can take
up to 25 ms per operation. Use one cooperative loop: the API is not thread-safe, and
interrupt handlers should record events for that loop. Public state/configuration
properties are read-only.

| Method | Contract |
| --- | --- |
| `connect()` | Begin asynchronous identity checks and RAM configuration from `disconnected`; update until `ready`. |
| `set_rpm(rpm)` | Select a magnitude from `5..max_rpm`, or zero to request a coast stop while active. Changing target does not start an idle motor. |
| `set_direction(+1/-1)` | Accept an integer direction only in configured, fresh, healthy, confirmed stationary IDLE, with no close pending or overdue loop. Does not start motion. |
| `configure_stall_recovery(enabled, detection_s=0.4)` | With the same stopped guard, allow an external supervisor time to respond. Enabled hard-stall fallback is `detection_s + 0.3` seconds; disabled is 0.25 seconds, after startup grace. Never reverses/restarts by itself. |
| `start()` | Request closed-loop entry at zero, confirm fresh feedback, then ramp toward target. Requires `ready`; acceptance does not mean target speed has been reached. |
| `stop()` | Request IDLE/coasting. Continue updates until fresh stationary feedback confirms `stop_confirmed`. |
| `clear_fault()` | Acknowledge the controller's local fault only with fresh healthy stationary IDLE, then reconnect/configure with target zero. Never sends drive clear-errors or starts motion. |
| `close()` | Stop, confirm stationary IDLE, disable/read back the watchdog owned by this instance, then close CAN. Update until `state == "closed"`. |
| `update()` | Advance bounded communication, configuration, supervision and stopping work. |
| `snapshot()` | Return detached JSON-friendly status; reading does not command motion. |

States are `disconnected`, `connecting`, `idle`, `starting`, `running`, `stopping`,
`fault` and `closed`. Keep faults and unconfirmed stops visible; a zero target or old
speed reading does not prove the drive is disabled. Keep the transport open while
stopping. The [loop example](examples/ui_loop.py) includes an asynchronous shutdown
service loop.

The **0.5 s drive watchdog remains enabled in IDLE** until orderly close. Stopping
service prematurely can leave drive error `0x800`, even on a stationary motor. Local
acknowledgement never clears that drive error.

## Feedback and load

| Snapshot fields | Meaning |
| --- | --- |
| `state`, `ready`, `running`, `fault` | Controller lifecycle and latched local fault. |
| `target_rpm`, `commanded_rpm` | Requested and ramped output-shaft speed magnitudes. |
| `measured_rpm`, `direction`, `supports_reverse` | Signed measured output RPM and selected integer direction/capability. |
| `load_percent`, `load_valid`, `load_source` | Running load proxy; source is `current_demand`. |
| `iq_setpoint_a`, `iq_measured_a` | Latest signed demanded/measured Q-axis motor current in amperes. |
| `iq_setpoint_filtered_a`, `iq_measured_filtered_a` | Signed low-pass current estimates, with a 200 ms filter time constant. |
| `current_limit_a`, `bus_voltage_v`, `bus_current_a` | Effective configured current cap and DC supply measurements. Bus current differs from Q-axis current. |
| `axis_state`, `axis_error`, `axis_flags` | Drive heartbeat status, separate from controller state. |
| `telemetry_valid`, `telemetry_age_s`, `stop_confirmed` | Feedback validity/age and fresh stationary IDLE confirmation. |
| `supports_stall_recovery`, `stall_recovery_enabled`, `stall_detection_s`, `stall_timeout_s` | External recovery capability and controller fallback configuration. |
| `diagnostics` | Frame counts, receive overflows, transmit failures, malformed frames and heartbeat continuity. |

Missing/stale measurements are `None`. Freshness limits are 300 ms for heartbeat, 150 ms
for encoder, 200 ms for Iq and 350 ms for bus telemetry. `load_valid` additionally
requires running closed loop and Iq received after that start. `telemetry_valid` alone
does not validate load. IDLE load is `None`, even if the drive repeats stored current
samples.

```text
load_percent = min(100, abs(filtered_signed_Iq_setpoint) / current_limit_a * 100)
output_rpm = rotor_rev_s * 60 / 8
```

Average with sign preserved before taking magnitude. Forward rotation produced negative
Iq in the fixture check. Load includes bearing, gearbox and fixture friction; it is not
torque, resistance or a calibrated percentage of grinder capacity. Raw measured current
is noisy and can briefly exceed the configured limit. That setting is not an
instantaneous measured-current ceiling.

## Runtime setup and transport

Defaults are `max_rpm=110`, `current_limit_a=0.5`, `gear_ratio=8`. Allowed maximum RPM
is 5–110, requested current is 0.05–1.0 A, and only the commissioned 8:1 ratio is
accepted. Positive targets below 5 RPM are rejected. Setup preserves a lower current cap
already in the drive.

The controller selects velocity/direct-input mode, zero torque feedforward, velocity
limiting and overspeed protection, with readback. The default drive speed cap is 130
output RPM. It requests heartbeat at 10 Hz, encoder at 50 Hz, Iq at 20 Hz and bus
voltage/current at 10 Hz, and checks a 20–27.5 V bus range. Targets ramp up at 25 RPM/s
and down at 20 RPM/s. `stop()` requests IDLE/coasting and waits for fresh IDLE before
zero-input keepalives. Stale feedback, wrong direction, overspeed, sustained
overcurrent, drive errors and active receive overflow latch a fault and initiate
stopping.

Changes are RAM-only: no calibration or flash-save command is sent. Closing disables the
watchdog owned by the controller but leaves conservative caps and telemetry
configuration in RAM. Power cycling restores saved drive configuration; connect and
verify again before motion.

The Waveshare Pico-CAN-B adapter uses a 16 MHz MCP2515 at 500 kbit/s: GP4 MISO, GP5 CS,
GP6 clock and GP7 MOSI. One-shot transmit mode, one pending frame, completion/deadline
checks and aborts prevent old queued motion frames from being replayed after
reconnection. Transport close restores listen-only mode.

That wiring conflicts with the UI encoder on GP4/GP5. The LCD and original CAN pin banks
also both select SPI0. A spacer does not resolve these conflicts; see the [UI
integration notes](grinder_ui/README.md#wiring-and-future-motor-integration).

## UI model and automation

`UiModel(backend=None, settings=None, clock=time)` defaults to `OfflineMotor`. An
injected backend supplies `set_rpm`, `start`, `stop`, `update`, `snapshot` and `close`;
the model does not call `connect()` automatically. Snapshot validity determines what can
be displayed. Demo data remains labelled SIMULATION, including while stopped.

`GrindAutomation` sits above the backend. The model calls `prepare()` before an explicit
start, `begin()` after it, and `update(snapshot, selected_rpm)` after backend updates.
Manual STOP calls `cancel()` before `backend.stop()` so no pending reverse/resume
survives. Both automatic features default off.

Autostop qualifies settled forward speed and sustained beans load, then counts
continuously low filtered load. Empty startup alone cannot finish a batch. Recovery
requires sustained near-zero speed and high fresh current demand, preferring unfiltered
`iq_setpoint_a/current_limit_a` for detection. It stops, confirms stationary IDLE,
reverses for a proved movement interval, stops/confirms again, then resumes the latest
selected forward target. Attempts and transition deadlines are bounded. Stale recovery
feedback and exhausted retries latch a local fault. Automation acknowledgement clears
only that local fault after a healthy confirmed stop, never a drive fault.

The automation sampling-gap guard defaults to 350 ms; the UI uses 750 ms only for
simulation to tolerate prototype LCD redraws. Live controller deadlines remain
unchanged. Physical reversal, beans-load thresholds and combined hardware/control
scheduling require commissioning. The [UI guide](grinder_ui/README.md) documents all
settings and persistence details.

## Installation and validation

Use the [project installation guide](../README.md) for the offline UI. API-only
development can copy `grinder_api` to `CIRCUITPY/lib/grinder_api` with matching
CircuitPython MCP2515 dependencies. Keep examples under their own filenames. Running
`verify_idle.main()` explicitly checks setup and ten seconds of stationary telemetry
before asynchronous close, without requesting motion.

The API completed an on-device IDLE-only check with 901 steady frames, zero steady
receive overflows and confirmed shutdown; startup overflow diagnostics remained visible.
Current direction/recovery behavior is exercised against a timed fake CAN peer.
Dedicated commissioning profiles supplied the physical motion/current measurements in
the [bring-up record](bringup/README.md). The current UI/menu/automation were checked on
the Pico with simulation and CAN unloaded, as recorded in the UI guide.

Run host checks from the repository root:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -q
python -m unittest discover -s Software/tests -q
python -m unittest discover -s Software/bringup/tests -q
```
