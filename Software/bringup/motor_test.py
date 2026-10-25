"""One-shot unloaded 24 V commissioning for SteadyWin firmware 0.5.14.

Run main(move=False) to inspect; main(move=True) performs one bounded trial.
Never install this as code.py. Runtime settings only; no flash-save/calibration.
Sources: vendor GIM6010-8 rev2.2 manual and endpoints_0.5.14.json.
"""

import math
import struct
import time

TARGET_RPM = 10.0
GEAR_RATIO = 8.0
ROTOR_LIMIT = 2.0
CURRENT_LIMIT = 1.0
WATCHDOG_SECONDS = 0.5
MOTION_SECONDS = 3.0
WD_ERROR = 0x800
FIELDS = {
    "fw_major": (9, "B"), "fw_minor": (10, "B"),
    "fw_revision": (11, "B"), "unreleased": (12, "B"),
    "voltage": (2, "f"), "calibrated": (192, "?"),
    "ready": (330, "?"), "velocity_limit": (289, "f"),
    "current_limit": (246, "f"), "watchdog_timeout": (150, "f"),
    "watchdog_enabled": (151, "?"), "control_mode": (283, "B"),
    "input_mode": (284, "B"), "input_velocity": (267, "f"),
    "input_torque": (268, "f"), "step_dir": (147, "?"),
    "sensorless": (149, "?"), "enable_vel_limit": (279, "?"),
    "enable_overspeed_error": (282, "?"),
}


class MotorTrial:
    fields = FIELDS
    target_rpm = TARGET_RPM
    rotor_limit = ROTOR_LIMIT
    current_limit = CURRENT_LIMIT

    def __init__(self, can, message_type, clock=time, emit=print,
                 tx_complete=None, abort_tx=None):
        self.can, self.message_type = can, message_type
        self.clock, self.emit = clock, emit
        self.tx_complete, self.abort_tx = tx_complete, abort_tx
        self.hb_seq = 0
        self.encoder_seq = 0
        self.last_hb = self.last_encoder = None
        self.error, self.flags, self.state = 0, 0, None
        self.position = self.velocity = 0.0
        self.reply = None
        self.allow_expiry = False
        self.watchdog_proven = self.limits_verified = False
        self.closed_loop_watchdog_proven = False
        self.changed = False
        self.original = {}

    def _send(self, command, data):
        if command not in (0x04, 0x05, 0x07, 0x0B, 0x0D, 0x0F, 0x18, 0x02):
            raise ValueError("Unsupported commissioning command")
        if command == 0x07 and struct.unpack("<I", data[:4])[0] == 8:
            if not self.watchdog_proven or not self.limits_verified:
                raise RuntimeError("Closed loop refused before watchdog/limit checks")
        if command == 0x0D:
            velocity, torque = struct.unpack("<ff", data)
            if not math.isfinite(velocity) or abs(velocity) > self.target_rpm * GEAR_RATIO / 60 + 0.00001 or torque != 0:
                raise ValueError("Invalid commissioning setpoint")
            if velocity and (not self.closed_loop_watchdog_proven or not self.limits_verified or self.state != 8):
                raise RuntimeError("Motion refused before closed-loop confirmation")
        if command == 0x18 and (self.error != WD_ERROR or self.flags & 0x0F or not self.allow_expiry):
            raise RuntimeError("Refusing to clear an unexpected fault")
        result = self.can.send(self.message_type(0x20 | command, data, extended=False))
        if result is False:
            raise RuntimeError("CAN frame was not queued")
        if self.tx_complete is not None:
            self.tx_complete()
        # Keep RX serviced while allowing the drive's CAN handler to process
        # configuration bursts. MCP2515 has only two hardware RX buffers.
        until = self.clock.monotonic() + 0.003
        while self.clock.monotonic() < until:
            self.pump()
            self.clock.sleep(0.001)

    def setpoint(self, velocity=0.0):
        self._send(0x0D, struct.pack("<ff", velocity, 0.0))

    def axis_state(self, state):
        if state not in (1, 8):
            raise ValueError("Only IDLE and closed-loop states are allowed")
        self._send(0x07, struct.pack("<II", state, 0))

    def pump(self):
        self.handle_frame(self.can.read_message())

    def handle_frame(self, frame):
        if frame is None or frame.extended or not hasattr(frame, "data"):
            return
        data = bytes(frame.data)
        if len(data) != 8:
            return
        now = self.clock.monotonic()
        if frame.id == 0x21:
            self.error = struct.unpack("<I", data[:4])[0]
            self.state, self.flags = data[4], data[5]
            self.last_hb = now
            self.hb_seq += 1
            if self.flags & 0x0F or (self.error and not (self.allow_expiry and self.error == WD_ERROR)):
                raise RuntimeError("Drive fault error=0x%X flags=0x%02X state=%d" % (self.error, self.flags, self.state))
        elif frame.id == 0x29:
            self.position, self.velocity = struct.unpack("<ff", data)
            if not math.isfinite(self.position) or not math.isfinite(self.velocity):
                raise RuntimeError("Invalid encoder feedback")
            self.last_encoder = now
            self.encoder_seq += 1
        elif frame.id == 0x24 and data[0] == 0 and data[3] == 0:
            self.reply = data

    def feedback_ok(self):
        now = self.clock.monotonic()
        if self.last_hb is None or self.last_encoder is None or now - self.last_hb > 0.25 or now - self.last_encoder > 0.25:
            raise RuntimeError("CAN feedback timeout")
        if abs(self.velocity) > self.rotor_limit * 1.25:
            raise RuntimeError("Measured speed exceeded test bound")

    def wait(self, predicate, seconds=0.4, feed=False):
        deadline = self.clock.monotonic() + seconds
        next_feed = self.clock.monotonic()
        while self.clock.monotonic() < deadline:
            self.pump()
            if predicate():
                return
            now = self.clock.monotonic()
            if feed and now >= next_feed:
                self.feedback_ok()
                self.setpoint()
                next_feed = now + 0.05
            self.clock.sleep(0.001)
        raise RuntimeError("Timed out waiting for drive feedback")

    def fresh_idle(self, feed=False):
        sequence = self.hb_seq
        encoder_sequence = self.encoder_seq
        self.wait(lambda: self.hb_seq > sequence and self.state == 1 and self.error == 0
                  and self.encoder_seq > encoder_sequence
                  and self.last_encoder is not None
                  and self.clock.monotonic() - self.last_encoder < 0.25, feed=feed)
        self.feedback_ok()
        if abs(self.velocity) > 0.15:
            raise RuntimeError("Motor is not stationary")

    def read(self, name):
        endpoint, kind = self.fields[name]
        for _ in range(4):
            self.pump()
        self.reply = None
        self._send(0x04, struct.pack("<BHB4s", 0, endpoint, 0, bytes(4)))
        try:
            self.wait(lambda: self.reply is not None and self.reply[1:3] == struct.pack("<H", endpoint))
        except RuntimeError as error:
            raise RuntimeError("Read %s (endpoint %d): %s" % (name, endpoint, error))
        # CircuitPython struct omits CPython's '?' boolean format.
        codec = "B" if kind == "?" else kind
        size = struct.calcsize("<" + codec)
        value = struct.unpack("<" + codec, self.reply[4:4 + size])[0]
        if kind == "?":
            if value not in (0, 1):
                raise RuntimeError("Invalid boolean endpoint: " + name)
            value = bool(value)
        if kind == "f" and not math.isfinite(value):
            raise RuntimeError("Invalid float endpoint: " + name)
        return value

    def write(self, name, value):
        if name not in ("watchdog_timeout", "watchdog_enabled", "enable_vel_limit", "enable_overspeed_error"):
            raise ValueError("Only runtime watchdog and limit-enable configuration is writable")
        if name in ("enable_vel_limit", "enable_overspeed_error") and value is not True:
            raise ValueError("Velocity protections may only be enabled")
        endpoint, kind = self.fields[name]
        encoded = struct.pack("<" + ("B" if kind == "?" else kind), value)
        self._send(0x05, struct.pack("<BHB", 1, endpoint, 0) + encoded + bytes(4 - len(encoded)))
        actual = self.read(name)
        if abs(actual - value) > 0.00001:
            raise RuntimeError("Readback mismatch: %s" % name)

    def inspect(self):
        self.fresh_idle()
        # Version is checked before accessing revision-dependent endpoints.
        values = {name: self.read(name) for name in ("fw_major", "fw_minor", "fw_revision", "unreleased")}
        if (values["fw_major"], values["fw_minor"], values["fw_revision"]) != (0, 5, 14):
            raise RuntimeError("Firmware endpoint table does not match 0.5.14")
        for name in self.fields:
            if name not in values:
                values[name] = self.read(name)
        self.original = values
        for name in self.fields:
            self.emit("CONFIG %s=%s" % (name, values[name]))
        return values

    def prove_watchdog(self):
        self.emit("WATCHDOG: zero-speed feed test in IDLE")
        self.write("watchdog_timeout", WATCHDOG_SECONDS)
        self.setpoint()
        self.allow_expiry = True
        self.write("watchdog_enabled", True)
        # Feed at zero for twice the timeout, then stop ALL addressed traffic.
        until = self.clock.monotonic() + WATCHDOG_SECONDS * 2.2
        next_feed = 0
        while self.clock.monotonic() < until:
            self.pump()
            self.feedback_ok()
            if self.state != 1 or self.error:
                raise RuntimeError("Watchdog did not remain healthy with zero-speed CAN feed")
            now = self.clock.monotonic()
            if now >= next_feed:
                self.setpoint()
                next_feed = now + 0.05
            self.clock.sleep(0.001)
        self.setpoint()
        started = self.clock.monotonic()
        self.emit("WATCHDOG: observing expiry with no outgoing commands")
        self.wait(lambda: self.error == WD_ERROR and self.state == 1, seconds=1.2)
        elapsed = self.clock.monotonic() - started
        self.feedback_ok()
        if elapsed < WATCHDOG_SECONDS * 0.6 or abs(self.velocity) > 0.15:
            raise RuntimeError("Watchdog expiry was early or motor was not stationary")
        self.emit("WATCHDOG VERIFIED: error=0x800 in %.3fs, state=IDLE" % elapsed)
        self.setpoint()  # Refresh the timer BEFORE clearing the expected fault.
        self._send(0x18, bytes(8))
        self.fresh_idle(feed=True)
        self.allow_expiry = False
        self.watchdog_proven = True

    def stop(self):
        self.emit("STOP: zero velocity and IDLE")
        failures = []
        if self.abort_tx:
            try:
                self.abort_tx()
            except Exception as error:
                failures.append(str(error))
        for operation in (self.setpoint, lambda: self.axis_state(1)):
            try:
                operation()
            except Exception as error:
                failures.append(str(error))
        self.allow_expiry = True
        sequence = self.hb_seq
        encoder_sequence = self.encoder_seq
        self.wait(lambda: self.hb_seq > sequence and self.encoder_seq > encoder_sequence
                  and self.state == 1 and abs(self.velocity) < 0.15, seconds=0.8)
        self.feedback_ok()
        # Restore watchdog settings only after confirmed idle/stationary. Keep
        # conservative current/speed limits and zero targets; never save flash.
        self.write("watchdog_enabled", self.original["watchdog_enabled"])
        self.write("watchdog_timeout", self.original["watchdog_timeout"])
        if failures:
            raise RuntimeError("Stop frame failure: " + "; ".join(failures))
        self.emit("STOP CONFIRMED: state=IDLE, output=%.3f RPM" % (self.velocity * 60 / GEAR_RATIO))

    def select_velocity_limit(self, values):
        return min(self.rotor_limit, values["velocity_limit"])

    def configure_extra(self):
        pass

    def perform_motion(self):
        target = self.target_rpm * GEAR_RATIO / 60
        self.emit("MOTION: target=%.1f output RPM, duration=3s" % self.target_rpm)
        started = self.clock.monotonic()
        start_position = self.position
        next_feed = next_log = started
        reached = False
        while self.clock.monotonic() - started < MOTION_SECONDS:
            self.pump()
            self.feedback_ok()
            if self.state != 8:
                raise RuntimeError("Drive left closed-loop state")
            now = self.clock.monotonic()
            if now >= next_feed:
                self.setpoint(target)
                next_feed = now + 0.05
            reached |= self.velocity >= target * 0.8
            if self.velocity < -0.2 or abs(self.position - start_position) > GEAR_RATIO:
                raise RuntimeError("Direction/travel exceeded commissioning bounds")
            if now - started > 1.5 and not reached:
                raise RuntimeError("Motor did not reach target; no automatic current increase")
            if now >= next_log:
                self.emit("RUN t=%.2fs output=%.3f RPM rotor=%.4f rev/s" % (now - started, self.velocity * 60 / GEAR_RATIO, self.velocity))
                next_log = now + 0.2
            self.clock.sleep(0.001)
        result = {"reached_target": reached, "output_revolutions": (self.position - start_position) / GEAR_RATIO}
        self.emit("MOTION END: output travel=%.4f revolutions" % result["output_revolutions"])
        return result

    def run(self, move=False):
        values = self.inspect()
        if not move:
            return values
        if not 20 <= values["voltage"] <= 28 or not values["calibrated"] or not values["ready"]:
            raise RuntimeError("24 V supply and calibrated motor/ready encoder required")
        if values["step_dir"] or values["sensorless"]:
            raise RuntimeError("Drive input configuration is unsuitable for this test")
        velocity_limit = self.select_velocity_limit(values)
        current_limit = min(self.current_limit, values["current_limit"])
        target = self.target_rpm * GEAR_RATIO / 60
        if not math.isfinite(velocity_limit) or not math.isfinite(current_limit) or velocity_limit < target * 1.1 or current_limit <= 0:
            raise RuntimeError("Existing current/speed limits do not permit the test")
        self.fresh_idle()
        failed = False
        try:
            self.changed = True
            self._send(0x0F, struct.pack("<ff", velocity_limit, current_limit))
            self.write("enable_vel_limit", True)
            self.write("enable_overspeed_error", True)
            self._send(0x0B, struct.pack("<II", 2, 1))
            self.setpoint()
            for name, expected in (("velocity_limit", velocity_limit), ("current_limit", current_limit), ("control_mode", 2), ("input_mode", 1), ("input_velocity", 0), ("input_torque", 0)):
                self.emit("VERIFY %s=%s" % (name, expected))
                if abs(self.read(name) - expected) > 0.00001:
                    raise RuntimeError("Safe setup readback mismatch: " + name)
            self.limits_verified = True
            self.configure_extra()
            self.prove_watchdog()
            self.setpoint()
            self.axis_state(8)
            sequence = self.hb_seq
            encoder_sequence = self.encoder_seq
            self.wait(lambda: self.hb_seq > sequence and self.encoder_seq > encoder_sequence
                      and self.state == 8, seconds=0.8, feed=True)
            self.emit("WATCHDOG: verifying closed-loop disarm at zero speed")
            self.allow_expiry = True
            self.setpoint()
            self.wait(lambda: self.error == WD_ERROR and self.state == 1, seconds=1.2)
            self.feedback_ok()
            if abs(self.velocity) > 0.15:
                raise RuntimeError("Nonzero speed during zero-target watchdog test")
            self.emit("CLOSED-LOOP WATCHDOG DISARM VERIFIED")
            self.setpoint()
            self._send(0x18, bytes(8))
            self.fresh_idle(feed=True)
            self.allow_expiry = False
            self.closed_loop_watchdog_proven = True
            self.setpoint()
            self.axis_state(8)
            sequence = self.hb_seq
            encoder_sequence = self.encoder_seq
            self.wait(lambda: self.hb_seq > sequence and self.encoder_seq > encoder_sequence
                      and self.state == 8, seconds=0.8, feed=True)
            self.feedback_ok()
            if abs(self.velocity) > 0.15:
                raise RuntimeError("Nonzero speed before commanded motion")
            return self.perform_motion()
        except BaseException:
            failed = True
            raise
        finally:
            if self.changed:
                try:
                    self.stop()
                except BaseException as error:
                    self.emit("STOP VERIFICATION FAILED: %s" % error)
                    if not failed:
                        raise


def main(move=False, trial_type=MotorTrial):
    import board
    import busio
    import digitalio
    from adafruit_mcp2515 import MCP2515
    from adafruit_mcp2515.canio import Message

    passive = {"__name__": "passive_registers"}
    with open("/code.py") as source:
        exec(source.read(), passive)
    cs = digitalio.DigitalInOut(board.GP5)
    cs.switch_to_output(value=True)
    spi = busio.SPI(clock=board.GP6, MOSI=board.GP7, MISO=board.GP4)
    registers = passive["Registers"](spi, cs)

    def abort_tx():
        registers.transfer(bytes((0x05, 0x0F, 0x10, 0x10)))
        deadline = time.monotonic() + 0.025
        while any(registers.read(address)[0] & 8 for address in (0x30, 0x40, 0x50)):
            if time.monotonic() > deadline:
                raise RuntimeError("CAN transmit abort failed")
            time.sleep(0.001)
        registers.transfer(bytes((0x05, 0x0F, 0x10, 0)))

    def tx_complete():
        deadline = time.monotonic() + 0.025
        while True:
            controls = [registers.read(address)[0] for address in (0x30, 0x40, 0x50)]
            if not any(value & 8 for value in controls):
                if any(value & 0x70 for value in controls):
                    abort_tx()
                    raise RuntimeError("CAN transmission failed")
                return
            if time.monotonic() > deadline:
                abort_tx()
                raise RuntimeError("CAN transmission deadline expired")
            time.sleep(0.001)

    try:
        registers.probe()
        can = MCP2515(spi, cs, silent=False, loopback=False,
                      baudrate=500_000, crystal_freq=16_000_000)
        # One-shot transmission prevents indefinite retries after bus loss.
        registers.transfer(bytes((0x05, 0x0F, 8, 8)))
        status, control = registers.read(0x0E, 2)
        if status & 0xE0 or control & 0xE0 or not control & 8 or tuple(registers.read(0x28, 3)) != (0x86, 0xF0, 0):
            raise RuntimeError("Normal CAN / one-shot / timing verification failed")
        trial = trial_type(can, Message, tx_complete=tx_complete, abort_tx=abort_tx)
        result = trial.run(move=move)
        print("TEST COMPLETE:", result)
        return result
    except BaseException as error:
        print("TEST STOP: %s: %s" % (type(error).__name__, error))
        try:
            print("MCP2515 EFLG=0x%02X" % registers.read(0x2D)[0])
        except Exception:
            pass
        raise
    finally:
        try:
            abort_tx()
        finally:
            registers.preserve_listen_only()
            spi.deinit()
            cs.deinit()


if __name__ == "__main__":
    main(move=False)
