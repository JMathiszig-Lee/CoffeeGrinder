"""Fixed unloaded 71 mm conical sweep; runtime settings only, no flash save.

Execute after motor_test.py in the same namespace. Vendor 0.5.14 endpoint
table: axis0.config.can.iq_rate_ms=186, bus_vi_rate_ms=188 (uint32).
"""

import math
import struct

try:
    MotorTrial
except NameError:
    from motor_test import MotorTrial, FIELDS, GEAR_RATIO, WD_ERROR


class ConicalSweep(MotorTrial):
    fields = dict(FIELDS, iq_rate_ms=(186, "I"), bus_vi_rate_ms=(188, "I"))
    target_rpm = 110.0
    rotor_limit = 130.0 * GEAR_RATIO / 60.0
    current_limit = 1.0
    stages = (20.0, 50.0, 80.0, 110.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_bus = self.last_iq = None
        self.vbus = self.ibus = self.iq = 0.0
        self.max_vbus = self.max_iq = 0.0
        self.samples = []
        self.overcurrent_since = None
        self.overcurrent_samples = 0
        self.overcurrent = False
        self.motion_start = self.motion_position = None
        self.command_rpm = 0.0
        self.next_log = 0.0

    def select_velocity_limit(self, values):
        return self.rotor_limit

    def write(self, name, value):
        if name not in ("iq_rate_ms", "bus_vi_rate_ms"):
            return super().write(name, value)
        if type(value) is not int or value not in (100, self.original.get(name)):
            raise ValueError("Only fixed telemetry rates and their original values are allowed")
        endpoint, _ = self.fields[name]
        self._send(0x05, struct.pack("<BHBI", 1, endpoint, 0, value))
        if self.read(name) != value:
            raise RuntimeError("Telemetry rate readback mismatch: " + name)

    def configure_extra(self):
        if self.state != 1:
            raise RuntimeError("Telemetry setup requires IDLE")
        self.write("iq_rate_ms", 100)
        self.write("bus_vi_rate_ms", 100)

    def handle_frame(self, frame):
        if (frame is not None and not frame.extended and hasattr(frame, "data")
                and len(frame.data) == 8 and frame.id in (0x34, 0x37)):
            first, second = struct.unpack("<ff", frame.data)
            if not math.isfinite(first) or not math.isfinite(second):
                raise RuntimeError("Invalid current/voltage telemetry")
            now = self.clock.monotonic()
            if frame.id == 0x37:
                self.vbus, self.ibus, self.last_bus = first, second, now
                self.max_vbus = max(self.max_vbus, first)
            else:
                self.iq, self.last_iq = second, now
                self.max_iq = max(self.max_iq, abs(second))
                if abs(second) > 1.2:
                    if self.overcurrent_since is None:
                        self.overcurrent_since = now
                    self.overcurrent_samples += 1
                    self.overcurrent = (self.overcurrent_samples >= 3 and
                                        now - self.overcurrent_since >= 0.2)
                else:
                    self.overcurrent_since = None
                    self.overcurrent_samples = 0
                    self.overcurrent = False
            return
        sequence = self.encoder_seq
        super().handle_frame(frame)
        if self.encoder_seq > sequence and self.motion_start is not None:
            now = self.last_encoder
            self.samples.append((now, self.position, self.velocity * 60 / GEAR_RATIO))
            while self.samples and (self.samples[0][0] < now - 1.1 or len(self.samples) > 160):
                self.samples.pop(0)

    def check_motion(self):
        self.pump()
        self.feedback_ok()
        now = self.clock.monotonic()
        if self.state != 8:
            raise RuntimeError("Sweep left closed-loop state")
        if (self.last_bus is None or self.last_iq is None or
                now - self.last_bus > 0.35 or now - self.last_iq > 0.35):
            raise RuntimeError("Current/voltage telemetry timeout")
        if not 20 <= self.vbus <= 27.5:
            raise RuntimeError("Bus voltage outside sweep bounds")
        if self.overcurrent:
            raise RuntimeError("Measured Iq exceeded the 1 A sweep allowance")
        rpm = self.velocity * 60 / GEAR_RATIO
        if rpm < -2 or rpm > 135:
            raise RuntimeError("Sweep direction/speed bound exceeded")
        if now - self.motion_start > 35 or abs(self.position - self.motion_position) / GEAR_RATIO > 60:
            raise RuntimeError("Sweep time/travel bound exceeded")
        if now >= self.next_log:
            self.emit("SWEEP_SAMPLE t=%.3f target=%.3f rpm=%.3f position=%.6f iq=%.4f vbus=%.3f ibus=%.4f"
                      % (now - self.motion_start, self.command_rpm, rpm,
                         self.position, self.iq, self.vbus, self.ibus))
            self.next_log = now + 0.2
        return now

    def segment(self, target, seconds, ramp=False):
        start_rpm = self.command_rpm
        started = self.clock.monotonic()
        next_feed = started
        while self.clock.monotonic() - started < seconds:
            now = self.check_motion()
            if now >= next_feed:
                fraction = min(1.0, (now - started) / seconds) if ramp else 1.0
                self.command_rpm = start_rpm + (target - start_rpm) * fraction
                self.setpoint(self.command_rpm * GEAR_RATIO / 60)
                next_feed = now + 0.05
            self.clock.sleep(0.001)
        self.command_rpm = target
        self.setpoint(target * GEAR_RATIO / 60)

    def stage_stats(self, target):
        now = self.clock.monotonic()
        samples = [sample for sample in self.samples if sample[0] >= now - 1.0]
        if len(samples) < 50 or samples[-1][0] - samples[0][0] < 0.8:
            raise RuntimeError("Insufficient fresh encoder samples for sweep stage")
        rpms = [sample[2] for sample in samples]
        mean = sum(rpms) / len(rpms)
        position_rpm = ((samples[-1][1] - samples[0][1]) / GEAR_RATIO * 60 /
                        (samples[-1][0] - samples[0][0]))
        allowance = max(3.0, target * 0.1)
        within = sum(abs(rpm - target) <= allowance for rpm in rpms) / len(rpms)
        tolerance = max(1.0, target * 0.05)
        passed = abs(mean - target) <= tolerance and abs(position_rpm - target) <= tolerance and within >= 0.9
        result = {"target": target, "meanrpm": mean, "minrpm": min(rpms),
                  "maxrpm": max(rpms), "positionrpm": position_rpm,
                  "samples": len(rpms), "pass": passed}
        self.emit("SWEEP_STAGE " + str(result))
        if not passed:
            raise RuntimeError("Sweep tracking failed at %.0f RPM; later stages refused" % target)
        return result

    def perform_motion(self):
        self.motion_start = self.clock.monotonic()
        self.motion_position = self.position
        self.samples = []
        results = []
        for target in self.stages:
            self.segment(target, abs(target - self.command_rpm) / 25.0, ramp=True)
            self.segment(target, 0.5)
            self.segment(target, 3.0 if target == 110 else 2.0)
            results.append(self.stage_stats(target))
        self.segment(0.0, self.command_rpm / 20.0, ramp=True)
        return {"stages": results, "max_vbus": self.max_vbus,
                "max_abs_iq": self.max_iq,
                "output_revolutions": (self.position - self.motion_position) / GEAR_RATIO}

    def stop(self):
        self.emit("COAST STOP: IDLE first, then zero target")
        failures = []
        self.allow_expiry = True
        if self.abort_tx:
            try:
                self.abort_tx()
            except Exception as error:
                failures.append(str(error))
        try:
            self.axis_state(1)
        except Exception as error:
            failures.append(str(error))
        # A lost IDLE command must never turn a zero target into active braking.
        # Observe IDLE, including watchdog fallback, before sending any zero.
        hb_sequence = self.hb_seq
        self.wait(lambda: self.hb_seq > hb_sequence and self.state == 1, seconds=1.2)
        try:
            self.setpoint()
        except Exception as error:
            failures.append(str(error))
        hb_sequence, encoder_sequence = self.hb_seq, self.encoder_seq
        self.wait(lambda: self.hb_seq > hb_sequence and self.encoder_seq > encoder_sequence
                  and self.state == 1 and abs(self.velocity) < 0.15, seconds=8.0, feed=True)
        self.feedback_ok()
        if self.error:
            raise RuntimeError("Coast stop retains watchdog fault; runtime configuration left conservative")
        for name in ("iq_rate_ms", "bus_vi_rate_ms"):
            self.write(name, self.original[name])
        current = min(self.current_limit, self.original["current_limit"])
        restore_speed = min(self.original["velocity_limit"], self.rotor_limit)
        self._send(0x0F, struct.pack("<ff", restore_speed, current))
        if abs(self.read("velocity_limit") - restore_speed) > 0.00001:
            raise RuntimeError("Stopped velocity limit restore failed")
        self.write("watchdog_enabled", self.original["watchdog_enabled"])
        self.write("watchdog_timeout", self.original["watchdog_timeout"])
        if failures:
            raise RuntimeError("Coast stop frame failure: " + "; ".join(failures))
        self.emit("COAST STOP CONFIRMED: stationary, stream rates restored, speed limit=%.6f rotor rev/s"
                  % restore_speed)
