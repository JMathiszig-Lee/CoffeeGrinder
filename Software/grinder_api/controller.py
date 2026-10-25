"""Nonblocking motor supervision; importing this module never touches hardware.

Call update continuously (every 1--5 ms), independently of display refreshes.
Requests change intent; snapshot reports confirmed drive state. Target and
commanded RPM are gearbox-output magnitudes; measured RPM retains its sign.
No flash writes, calibration, or automatic restart.
"""

import math
import struct
import time

from . import protocol


class _Fault(Exception):
    pass


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite number")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(name + " must be a finite number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("%s must be between %s and %s" % (name, low, high))
    return float(value)


class GrinderController:
    """One CAN node, owned by one cooperative UI loop.

    connect() configures a stationary motor asynchronously. start() is a
    separate action. stop()/close() also complete through subsequent updates.
    A fault is latched; clear_fault() only acknowledges a healthy, stopped drive
    and never sends the drive's clear-errors command.
    """

    _AGES = {"heartbeat": 0.30, "encoder": 0.15, "iq": 0.20, "bus": 0.35}
    _PERIOD = 0.05
    _WATCHDOG = 0.5
    _MAX_GAP = 0.1
    _STATIONARY_RPM = 1.125  # Same 0.15 rotor rev/s bound as commissioning.

    def __init__(self, transport, node_id=1, max_rpm=110.0,
                 current_limit_a=0.5, gear_ratio=8.0, clock=time):
        protocol.command_id(node_id, 1)
        self.transport, self.node_id, self.clock = transport, node_id, clock
        # This implementation is deliberately limited to the commissioned range.
        self._max_rpm = _number(max_rpm, "max_rpm", 5, 110)
        self._requested_current_limit = _number(current_limit_a, "current_limit_a", 0.05, 1.0)
        self._gear_ratio = _number(gear_ratio, "gear_ratio", 8, 8)
        self._current_limit_a = None
        self._limit_rpm = max(20.0, self._max_rpm * 130.0 / 110.0)
        self._state = "disconnected"
        self._fault = None
        self._target_rpm = 0.0
        self._commanded_rpm = 0.0
        self._direction = 1
        self._stall_recovery_enabled = False
        self._stall_detection_s = 0.4
        self._stall_timeout_s = 0.25
        self._stop_confirmed = False
        self._close_requested = False
        self._values = {}
        self._plan, self._waiting = [], None
        self._phase = None
        self._last_update = None
        self._next_tx = 0.0
        self._last_feed = None
        self._hb_seq = self._enc_seq = 0
        self._stamps = {key: None for key in self._AGES}
        self._axis_state = None
        self._axis_error = self._flags = 0
        self._rpm = self._position = None
        self._iq_set = self._iq_measured = None
        self._iq_set_filtered = self._iq_measured_filtered = None
        self._vbus = self._ibus = None
        self._configured = False
        self._watchdog_owned = False
        self._close_phase = None
        self._zero_safe = False
        self._stop_idle = False
        self._idle_seq = self._stop_enc_seq = 0
        self._stop_started = self._deadline = 0.0
        self._running_since = None
        self._stall_since = self._overcurrent_since = None
        self._overcurrent_samples = 0
        self._overflow_seen = 0
        self._invalid_frames = self._heartbeat_misses = self._heartbeat_duplicates = 0
        self._heartbeat_life = None

    @property
    def state(self):
        return self._state

    @property
    def fault(self):
        return self._fault

    @property
    def max_rpm(self):
        return self._max_rpm

    @property
    def gear_ratio(self):
        return self._gear_ratio

    @property
    def current_limit_a(self):
        return self._current_limit_a

    @property
    def requested_current_limit(self):
        return self._requested_current_limit

    @property
    def target_rpm(self):
        return self._target_rpm

    @property
    def commanded_rpm(self):
        return self._commanded_rpm

    @property
    def direction(self):
        return self._direction

    @property
    def supports_reverse(self):
        return True

    @property
    def supports_stall_recovery(self):
        return True

    @property
    def stop_confirmed(self):
        return self._stop_confirmed and self._stationary(self.clock.monotonic())

    def connect(self):
        """Begin identity checks/configuration; leaves the motor in IDLE."""
        if self._state != "disconnected":
            raise RuntimeError("connect requires disconnected state")
        now = self.clock.monotonic()
        self._state, self._phase = "connecting", "idle"
        self._stop_confirmed = False
        self._configured = self._zero_safe = False
        self._stamps = {key: None for key in self._AGES}
        self._values, self._plan, self._waiting = {}, [], None
        self._heartbeat_life = None
        self._deadline = now + 1.0
        self._last_update = now
        self._overflow_seen = getattr(self.transport, "rx_overflows", 0)

    def set_rpm(self, rpm):
        """Set output-shaft speed magnitude; zero requests a coast stop."""
        rpm = _number(rpm, "rpm", 0, self._max_rpm)
        if 0 < rpm < 5:
            raise ValueError("rpm must be zero (stop) or at least 5")
        if self._state == "closed":
            raise RuntimeError("Controller is closed")
        self._target_rpm = rpm
        if rpm == 0 and self._state in ("starting", "running"):
            self.stop()

    def _require_stationary_idle(self, now):
        if (self._state != "idle" or not self._configured or self._fault is not None
                or self._close_requested):
            raise RuntimeError("Motor is not ready for an IDLE configuration change")
        if (not self._stop_confirmed or not self._fresh(now) or not self._stationary(now)
                or self._axis_error or self._flags & 0x0F):
            raise RuntimeError("Fresh healthy stationary IDLE feedback is required")
        if self._last_update is None or now - self._last_update > self._MAX_GAP:
            raise RuntimeError("Controller update loop is overdue")

    def set_direction(self, direction):
        """Select +1 or -1 after a confirmed stop; never starts the motor."""
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError("direction must be +1 or -1")
        self._require_stationary_idle(self.clock.monotonic())
        self._direction = direction

    def configure_stall_recovery(self, enabled, detection_s=0.4):
        """Allow an external recovery response before the hard-stall fallback.

        Only a healthy stationary IDLE may change this setting. Enabled adds
        0.3 seconds to detection_s for the external supervisor to stop; it does
        not clear faults, reverse, or restart. Disabled retains the 0.25-second
        hard-stall timeout. The existing one-second startup grace is unchanged.
        """
        if type(enabled) is not bool:
            raise ValueError("enabled must be True or False")
        detection_s = _number(detection_s, "detection_s", 0.1, 3.0)
        self._require_stationary_idle(self.clock.monotonic())
        self._stall_recovery_enabled = enabled
        self._stall_detection_s = detection_s
        self._stall_timeout_s = detection_s + 0.3 if enabled else 0.25

    def start(self):
        """Request motion at the selected RPM; cannot queue an early start."""
        now = self.clock.monotonic()
        if self._state != "idle" or not self._configured or self._close_requested:
            raise RuntimeError("Motor is not ready")
        if self._target_rpm <= 0:
            raise ValueError("Select a positive RPM before starting")
        if not self._fresh(now) or self._axis_state != 1 or not self._stop_confirmed:
            raise RuntimeError("Fresh stationary IDLE feedback is required")
        if self._last_update is None or now - self._last_update > self._MAX_GAP:
            raise RuntimeError("Controller update loop is overdue")
        self._state, self._phase = "starting", "zero"
        self._stop_confirmed = False
        self._commanded_rpm = 0.0
        self._deadline = now + 1.0

    def stop(self):
        """Request IDLE before zeroing the target; call update until stopped."""
        if self._state in ("closed", "disconnected"):
            return
        if self._state == "fault":
            return  # Its stop attempt remains active without clearing the latch.
        self._begin_stop(self.clock.monotonic())

    def clear_fault(self):
        """Acknowledge a local fault. Drive errors require separate diagnosis."""
        now = self.clock.monotonic()
        if self._state != "fault":
            raise RuntimeError("No latched fault")
        if (not self._stop_confirmed or not self._fresh(now) or self._axis_error
                or self._flags & 0x0F or self._axis_state != 1):
            raise RuntimeError("Fresh healthy stationary IDLE feedback is required")
        self._fault = None
        self._target_rpm = self._commanded_rpm = 0.0
        self._configured = False
        self._state = "disconnected"
        self.connect()

    def close(self):
        """Request stop then release CAN after confirmed stationary IDLE."""
        if self._state == "closed":
            return
        self._close_requested = True
        if self._state == "disconnected" and not self._watchdog_owned:
            self.transport.close()
            self._state = "closed"
        elif self._state == "disconnected":
            self._begin_stop(self.clock.monotonic())
        elif not self._stop_confirmed:
            self.stop()
        # Otherwise update does the final zero/TX completion before closing.

    def _diagnostics(self):
        return getattr(self.transport, "diagnostics", {})

    def _fresh(self, now, names=None):
        for name in names or self._AGES:
            stamp = self._stamps[name]
            if stamp is None or now - stamp > self._AGES[name]:
                return False
        return True

    def _stationary(self, now):
        return (self._fresh(now, ("heartbeat", "encoder")) and
                self._axis_state == 1 and abs(self._rpm) < self._STATIONARY_RPM)

    def _send(self, command, payload, now):
        if self.transport.pending or now < self._next_tx:
            return False
        if not self.transport.send(protocol.command_id(self.node_id, command), payload):
            return False
        self._next_tx = now + 0.003
        if command == 0x0D:
            self._last_feed = now
        return True

    def _velocity(self, rpm, now):
        rpm = _number(rpm, "commanded RPM", 0, self._max_rpm)
        signed_rpm = rpm * self._direction if rpm else 0.0
        return self._send(0x0D, struct.pack("<ff", protocol.rotor_velocity(signed_rpm, self._gear_ratio), 0.0), now)

    def _axis(self, state, now):
        return self._send(0x07, struct.pack("<II", state, 0), now)

    def _begin_stop(self, now):
        self._plan, self._waiting = [], None
        self._phase = None
        self._configured = False if self._state == "connecting" else self._configured
        self._state = "fault" if self._fault else "stopping"
        self._stop_confirmed = False
        self._stop_idle = self._zero_safe = False
        self._close_phase = None
        self._stop_started = now
        self._idle_seq, self._stop_enc_seq = self._hb_seq, self._enc_seq
        self._next_tx = now
        self._stop_last_request = None
        try:
            self.transport.abort()
        except Exception:
            # Drive-side watchdog remains the fallback if the bus cannot send.
            pass

    def _fail(self, reason, now):
        if self._fault is None:
            self._fault = str(reason)
            self._begin_stop(now)

    def _handle(self, frame, now):
        can_id, data = frame
        if can_id >> 5 != self.node_id:
            return
        command = can_id & 0x1F
        if command == 0x04:
            if (self._waiting is not None and len(data) == 8 and data[0] == 0
                    and data[1:3] == struct.pack("<H", protocol.FIELDS[self._waiting[0]][0])):
                name, expected, _deadline = self._waiting
                value = protocol.read_reply(name, data)
                if expected is not None and abs(value - expected) > 0.0001:
                    raise _Fault("Configuration readback mismatch: " + name)
                self._values[name] = value
                self._waiting = None
                self._plan.pop(0)
            return
        if command not in (1, 9, 0x14, 0x17):
            return
        try:
            values = protocol.decode_telemetry(command, data)
        except ValueError:
            self._invalid_frames += 1
            raise _Fault("Malformed motor telemetry")
        if command == 1:
            life = data[7]
            if self._heartbeat_life is not None:
                gap = (life - self._heartbeat_life) & 0xFF
                if gap == 0:
                    self._heartbeat_duplicates += 1
                    if values[0] or values[2] & 0x0F:
                        self._axis_error, self._axis_state, self._flags = values
                        raise _Fault("Drive fault in duplicate heartbeat")
                    return
                self._heartbeat_misses += gap - 1
            self._axis_error, self._axis_state, self._flags = values
            self._heartbeat_life = life
            self._stamps["heartbeat"] = now
            self._hb_seq += 1
            if self._axis_error or self._flags & 0x0F:
                raise _Fault("Drive error 0x%X, flags 0x%02X" % (self._axis_error, self._flags))
        elif command == 9:
            self._position, velocity = values
            self._rpm = protocol.output_rpm(velocity, self._gear_ratio)
            self._stamps["encoder"] = now
            self._enc_seq += 1
        elif command == 0x14:
            previous = self._stamps["iq"]
            self._iq_set, self._iq_measured = values
            # Average signed samples first; forward motion produced negative Iq.
            alpha = 1.0 if previous is None else min(1.0, max(0.0, (now - previous) / (0.2 + now - previous)))
            if previous is None or now - previous > self._AGES["iq"]:
                self._iq_set_filtered, self._iq_measured_filtered = values
            else:
                self._iq_set_filtered += alpha * (values[0] - self._iq_set_filtered)
                self._iq_measured_filtered += alpha * (values[1] - self._iq_measured_filtered)
            self._stamps["iq"] = now
            high = self._current_limit_a is not None and abs(self._iq_measured) > self._current_limit_a * 1.3
            if high:
                if self._overcurrent_since is None:
                    self._overcurrent_since = now
                self._overcurrent_samples += 1
            else:
                self._overcurrent_since = None
                self._overcurrent_samples = 0
        else:
            self._vbus, self._ibus = values
            self._stamps["bus"] = now

    def _setup(self, now):
        if self._phase == "idle":
            if self._fresh(now, ("heartbeat", "encoder")):
                if not self._stationary(now):
                    raise _Fault("Connection requires stationary IDLE")
                self._plan = [("read", name, None) for name in
                              ("fw_major", "fw_minor", "fw_revision", "unreleased")]
                self._plan.append(("firmware",))
                self._phase = "config"
            elif now > self._deadline:
                raise _Fault("No fresh heartbeat/encoder on connection")
            return
        if not self._fresh(now, ("heartbeat", "encoder")) or not self._stationary(now):
            raise _Fault("Stationary IDLE feedback lost during configuration")
        if self._waiting is not None:
            if now > self._waiting[2]:
                raise _Fault("Configuration reply timeout: " + self._waiting[0])
            return
        if not self._plan:
            if self._phase != "telemetry":
                self._phase, self._deadline = "telemetry", now + 0.8
            if self._fresh(now):
                if not 20 <= self._vbus <= 27.5:
                    raise _Fault("24 V supply outside configured bounds")
                self._configured = self._stop_confirmed = True
                self._state, self._phase = "idle", None
            elif now > self._deadline:
                raise _Fault("Configured telemetry streams did not arrive")
            return
        step = self._plan[0]
        if step[0] == "firmware":
            version = tuple(self._values[key] for key in ("fw_major", "fw_minor", "fw_revision"))
            if version != (0, 5, 14):
                raise _Fault("Unsupported firmware; expected 0.5.14")
            self._plan.pop(0)
            self._plan.extend(("read", name, None) for name in
                              ("calibrated", "ready", "step_dir", "sensorless", "current_limit", "voltage"))
            self._plan.append(("prepare",))
        elif step[0] == "prepare":
            values = self._values
            if not values["calibrated"] or not values["ready"] or values["step_dir"] or values["sensorless"]:
                raise _Fault("Drive calibration/input configuration is unsuitable")
            if not 20 <= values["voltage"] <= 27.5 or values["current_limit"] <= 0:
                raise _Fault("Drive supply/current configuration is unsuitable")
            self._current_limit_a = min(self._requested_current_limit, values["current_limit"])
            self._plan.pop(0)
            self._configuration_plan()
            self._zero_safe = True
        elif step[0] == "read":
            if self._send(0x04, protocol.read_request(step[1]), now):
                self._waiting = (step[1], step[2], now + 0.4)
        elif step[0] == "write":
            if self._send(0x05, protocol.write_request(step[1], step[2]), now):
                if step[1] == "watchdog_enabled" and step[2]:
                    self._watchdog_owned = True
                self._plan.pop(0)
        elif step[0] == "send":
            if self._send(step[1], step[2], now):
                self._plan.pop(0)

    def _configuration_plan(self):
        limit = protocol.rotor_velocity(self._limit_rpm, self._gear_ratio)
        self._plan.append(("send", 0x0D, struct.pack("<ff", 0.0, 0.0)))
        self._plan.append(("send", 0x0F, struct.pack("<ff", limit, self._current_limit_a)))
        self._plan.extend((("read", "velocity_limit", limit), ("read", "current_limit", self._current_limit_a)))
        for name in ("enable_vel_limit", "enable_overspeed_error"):
            self._plan.extend((("write", name, True), ("read", name, True)))
        self._plan.append(("send", 0x0B, struct.pack("<II", 2, 1)))
        self._plan.extend(("read", name, expected) for name, expected in
                          (("control_mode", 2), ("input_mode", 1), ("input_velocity", 0.0), ("input_torque", 0.0)))
        for name, value in (("heartbeat_rate_ms", 100), ("encoder_rate_ms", 20),
                            ("iq_rate_ms", 50), ("bus_vi_rate_ms", 100),
                            ("watchdog_timeout", self._WATCHDOG)):
            self._plan.extend((("write", name, value), ("read", name, value)))
        self._plan.append(("send", 0x0D, struct.pack("<ff", 0.0, 0.0)))
        self._plan.extend((("write", "watchdog_enabled", True), ("read", "watchdog_enabled", True)))

    def _start_step(self, now):
        if now > self._deadline:
            raise _Fault("Closed-loop entry timeout")
        if self._phase == "zero":
            if self._velocity(0.0, now):
                self._phase = "arm"
        elif self._phase == "arm":
            if self._axis(8, now):
                self._idle_seq, self._stop_enc_seq = self._hb_seq, self._enc_seq
                self._phase = "confirm"
        elif (self._hb_seq > self._idle_seq and self._enc_seq > self._stop_enc_seq
              and self._axis_state == 8):
            if abs(self._rpm) >= self._STATIONARY_RPM:
                raise _Fault("Motion detected before nonzero command")
            self._state, self._phase = "running", None
            self._running_since = now
            self._stall_since = self._overcurrent_since = None
            self._overcurrent_samples = 0
            self._last_feed = now

    def _supervise(self, now):
        if not self._fresh(now):
            raise _Fault("Motor telemetry stale")
        if not 20 <= self._vbus <= 27.5:
            raise _Fault("Bus voltage outside 24 V operating bounds")
        directed_rpm = self._rpm * self._direction
        if directed_rpm < -2.0 or abs(self._rpm) > self._limit_rpm * 1.04:
            raise _Fault("Measured speed/direction outside limits")
        if self._state == "idle" and not self._stationary(now):
            raise _Fault("Drive left stationary IDLE")
        if self._state == "running":
            if self._axis_state != 8:
                raise _Fault("Drive left closed-loop control")
            slow = (self._commanded_rpm >= 5 and now - self._running_since > 1.0
                    and directed_rpm < min(3.0, self._commanded_rpm * 0.25))
            self._stall_since = (now if self._stall_since is None else self._stall_since) if slow else None
            if self._stall_since is not None and now - self._stall_since >= self._stall_timeout_s:
                raise _Fault("Motor stalled; no automatic restart")
            if (self._overcurrent_since is not None and self._overcurrent_samples >= 3
                    and self._stamps["iq"] - self._overcurrent_since >= 0.2):
                raise _Fault("Sustained measured current above allowance")

    def _feed(self, now):
        if self._last_feed is not None and now - self._last_feed < self._PERIOD:
            return
        rpm = 0.0
        if self._state == "running":
            dt = min(self._MAX_GAP, now - (self._last_feed if self._last_feed is not None else now))
            delta = self._target_rpm - self._commanded_rpm
            rate = 25.0 if delta >= 0 else 20.0
            rpm = self._commanded_rpm + max(-rate * dt, min(rate * dt, delta))
        if self._velocity(rpm, now):
            self._commanded_rpm = rpm

    def _stop_step(self, now):
        # Wait for a post-request heartbeat before any zero command. Repeating
        # IDLE too frequently could itself feed the watchdog if those frames
        # are lost by the drive handler. After an initial 200 ms, retry only
        # once a second, leaving enough silence for the 0.5-second watchdog.
        if self._hb_seq > self._idle_seq:
            self._stop_idle = self._axis_state == 1 and self._fresh(now, ("heartbeat",))
        if not self._stop_idle:
            retry_period = 0.05 if now - self._stop_started < 0.2 else 1.0
            if self._stop_last_request is None or now - self._stop_last_request >= retry_period:
                if self._axis(1, now):
                    self._stop_last_request = now
            if now - self._stop_started > 1.2 and self._fault is None:
                self._fault, self._state = "Stop not confirmed by drive", "fault"
            return
        self._zero_safe = True
        self._feed(now)
        if (self._enc_seq > self._stop_enc_seq and self._stationary(now)
                and self._last_feed is not None and self._last_feed >= self._stop_started
                and not self.transport.pending):
            self._stop_confirmed = True
            if self._fault is None:
                self._state = "idle" if self._configured else "disconnected"
        elif now - self._stop_started > 8 and self._fault is None:
            self._fault, self._state = "Motor did not coast to a confirmed stop", "fault"

    def _close_step(self, now):
        # Disable only the watchdog this instance enabled, and only after the
        # physical stop is confirmed. Leaving it enabled after releasing CAN
        # would create a needless sticky watchdog error on a stationary drive.
        if not self._watchdog_owned:
            if not self.transport.pending:
                self.transport.close()
                self._state = "closed"
            return
        if self._close_phase is None:
            self._close_phase = "disable"
            self._plan = [("read", "watchdog_enabled", False)]
        if self._waiting is not None:
            if now > self._waiting[2]:
                raise _Fault("Watchdog shutdown readback timeout")
            return
        if self._close_phase == "disable":
            if self._send(0x05, protocol.write_request("watchdog_enabled", False), now):
                self._close_phase = "readback"
        elif self._plan:
            if self._send(0x04, protocol.read_request("watchdog_enabled"), now):
                self._waiting = ("watchdog_enabled", False, now + 0.4)
        elif not self.transport.pending:
            self._watchdog_owned = False
            self.transport.close()
            self._state = "closed"

    def update(self):
        """Advance bounded work, drain RX and supervise before any keepalive."""
        if self._state == "closed":
            return
        now = self.clock.monotonic()
        # Buffered frames carry no trustworthy receive time. A UI pause must
        # fault before those frames can be restamped or feed a running drive.
        if (self._state in ("starting", "running") and self._last_update is not None
                and now - self._last_update > self._MAX_GAP):
            self._fail("Controller update loop missed its deadline", now)
        self._last_update = now
        try:
            self.transport.update()
            for _ in range(32):
                frame = self.transport.receive()
                if frame is None:
                    break
                try:
                    self._handle(frame, now)
                except (ValueError, _Fault) as error:
                    if self._state != "disconnected" or self._watchdog_owned:
                        self._fail(error, now)
            # Read the counter directly; constructing a diagnostics dictionary
            # on every fast-loop iteration adds avoidable GC pauses on Pico.
            overflows = getattr(self.transport, "rx_overflows", 0)
            if overflows > self._overflow_seen and self._state in ("starting", "running"):
                self._fail("CAN receive overflow during motion", now)
            self._overflow_seen = overflows
            if self._state == "connecting":
                self._setup(now)
                if self._zero_safe:
                    self._feed(now)
            elif self._state in ("idle", "starting", "running"):
                self._supervise(now)
                if self._state == "starting":
                    self._start_step(now)
                self._feed(now)
            elif self._state in ("stopping", "fault"):
                self._stop_step(now)
            elif self._state == "disconnected" and self._watchdog_owned:
                # A cancelled late connection may already have enabled the
                # watchdog. Continue zero-speed service until reconnect/close.
                if not self._stationary(now):
                    raise _Fault("Stopped connection lost IDLE feedback")
                self._feed(now)
            if self._close_requested and self._stop_confirmed and self._stationary(now):
                self._close_step(now)
        except Exception as error:
            self._fail(error, now)
            # No further addressed traffic here: a broken bus must expire the
            # drive-side watchdog. Future updates can resume the IDLE attempt.

    def snapshot(self):
        """Return a detached JSON-friendly view; missing/stale values are None."""
        now = self.clock.monotonic()
        valid = {key: self._fresh(now, (key,)) for key in self._AGES}
        iq_valid = valid["iq"]
        load = None
        if (iq_valid and valid["heartbeat"] and self._axis_state == 8
                and self._state == "running" and self._current_limit_a is not None
                and self._stamps["iq"] >= self._running_since):
            load = min(100.0, abs(self._iq_set_filtered) / self._current_limit_a * 100.0)
        diagnostics = dict(self._diagnostics())
        diagnostics.update({"invalid_frames": self._invalid_frames,
                            "heartbeat_misses": self._heartbeat_misses,
                            "heartbeat_duplicates": self._heartbeat_duplicates})
        return {
            "state": self._state,
            "ready": self._state == "idle" and self._configured and self._fresh(now) and self._stop_confirmed,
            "running": self._state == "running" and self._fresh(now) and self._axis_state == 8,
            "target_rpm": self._target_rpm,
            "commanded_rpm": self._commanded_rpm,
            "direction": self._direction,
            "supports_reverse": self.supports_reverse,
            "supports_stall_recovery": self.supports_stall_recovery,
            "stall_recovery_enabled": self._stall_recovery_enabled,
            "stall_detection_s": self._stall_detection_s,
            "stall_timeout_s": self._stall_timeout_s,
            "measured_rpm": self._rpm if valid["encoder"] else None,
            "load_percent": load,
            "load_valid": load is not None,
            "load_source": "current_demand",
            "iq_setpoint_a": self._iq_set if iq_valid else None,
            "iq_measured_a": self._iq_measured if iq_valid else None,
            "iq_setpoint_filtered_a": self._iq_set_filtered if iq_valid else None,
            "iq_measured_filtered_a": self._iq_measured_filtered if iq_valid else None,
            "current_limit_a": self._current_limit_a,
            "bus_voltage_v": self._vbus if valid["bus"] else None,
            "bus_current_a": self._ibus if valid["bus"] else None,
            "axis_state": self._axis_state if valid["heartbeat"] else None,
            "axis_error": self._axis_error if valid["heartbeat"] else None,
            "axis_flags": self._flags if valid["heartbeat"] else None,
            "fault": self._fault,
            "stop_confirmed": self._stop_confirmed and self._stationary(now),
            "telemetry_valid": all(valid.values()),
            "telemetry_age_s": {key: None if value is None else max(0.0, now - value)
                                for key, value in self._stamps.items()},
            "diagnostics": diagnostics,
        }
