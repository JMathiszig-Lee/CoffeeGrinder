"""Fixed current telemetry profile for a secured, guarded load fixture.

Definitions only: importing this file never starts a motor. Execute after
motor_test.py and conical_sweep.py in the same namespace, then select this
class explicitly. Iq is reported in amperes; no torque or load calibration.
"""

import math
import struct

try:
    ConicalSweep
except NameError:
    from conical_sweep import ConicalSweep, GEAR_RATIO


class CurrentSense(ConicalSweep):
    target_rpm = 10.0
    rotor_limit = 2.0
    current_limit = 0.5
    load_seconds = 60.0
    max_motion_seconds = 75.0
    travel_limit = 15.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phase = "ramp"
        self.iq_setpoint = self.iq_mean = self.iq_set_mean = 0.0
        self.iq_window = []
        self.phase_totals = {"baseline": [0.0, 0.0, 0], "load": [0.0, 0.0, 0]}
        self.phase_rpm = {"baseline": [0.0, 0], "load": [0.0, 0]}
        self.phase_blocks = {"baseline": [], "load": []}
        self.phase_started = None
        self.stable_since = None
        self.stall_armed = False
        self.stall_since = None
        self.stalled = False
        self.high_iq_since = None
        self.high_iq_samples = 0
        self.current_exceeded = False

    def select_velocity_limit(self, values):
        return min(self.rotor_limit, values["velocity_limit"])

    def write(self, name, value):
        if name != "iq_rate_ms":
            return super().write(name, value)
        if type(value) is not int or value not in (20, self.original.get(name)):
            raise ValueError("Only 20 ms Iq telemetry and its original rate are allowed")
        self._send(0x05, struct.pack("<BHBI", 1, 186, 0, value))
        if self.read(name) != value:
            raise RuntimeError("Current telemetry rate readback mismatch")

    def configure_extra(self):
        if self.state != 1:
            raise RuntimeError("Current telemetry setup requires IDLE")
        self.write("iq_rate_ms", 20)
        self.write("bus_vi_rate_ms", 100)

    def handle_frame(self, frame):
        sequence = self.encoder_seq
        super().handle_frame(frame)
        if (frame is not None and not frame.extended and hasattr(frame, "data")
                and len(frame.data) == 8 and frame.id == 0x34):
            self.iq_setpoint, measured = struct.unpack("<ff", frame.data)
            now = self.last_iq
            self.iq_window.append((now, self.iq_setpoint, measured))
            while self.iq_window and (self.iq_window[0][0] < now - 0.2 or len(self.iq_window) > 32):
                self.iq_window.pop(0)
            self.iq_mean = sum(sample[2] for sample in self.iq_window) / len(self.iq_window)
            self.iq_set_mean = sum(sample[1] for sample in self.iq_window) / len(self.iq_window)
            if self.motion_start is not None and self.phase in self.phase_totals:
                total = self.phase_totals[self.phase]
                total[0] += self.iq_setpoint
                total[1] += measured
                total[2] += 1
                bucket = int((now - self.phase_started) / 0.5)
                blocks = self.phase_blocks[self.phase]
                if not blocks or blocks[-1][0] != bucket:
                    blocks.append([bucket, now, now, 0.0, 0.0, 0])
                block = blocks[-1]
                block[2] = now
                block[3] += self.iq_setpoint
                block[4] += measured
                block[5] += 1
            if abs(measured) > 0.65:
                if self.high_iq_since is None:
                    self.high_iq_since = now
                self.high_iq_samples += 1
                self.current_exceeded = (self.high_iq_samples >= 3 and now - self.high_iq_since >= 0.2)
            else:
                self.high_iq_since = None
                self.high_iq_samples = 0
                self.current_exceeded = False
        if self.encoder_seq > sequence and self.motion_start is not None:
            rpm = self.velocity * 60 / GEAR_RATIO
            if rpm >= 8:
                if self.stable_since is None:
                    self.stable_since = self.last_encoder
                if self.last_encoder - self.stable_since >= 0.3:
                    self.stall_armed = True
            elif not self.stall_armed:
                self.stable_since = None
            if self.phase in self.phase_rpm:
                total = self.phase_rpm[self.phase]
                total[0] += rpm
                total[1] += 1
            monitor = self.stall_armed and self.phase in ("baseline", "load")
            if monitor and self.velocity * 60 / GEAR_RATIO < self.target_rpm * 0.3:
                if self.stall_since is None:
                    self.stall_since = self.last_encoder
                self.stalled = self.last_encoder - self.stall_since >= 0.2
            else:
                self.stall_since = None
                self.stalled = False

    def check_motion(self):
        self.pump()
        self.feedback_ok()
        now = self.clock.monotonic()
        if self.state != 8:
            raise RuntimeError("Current profile left closed-loop state")
        if (self.last_bus is None or self.last_iq is None or
                now - self.last_bus > 0.35 or now - self.last_iq > 0.15):
            raise RuntimeError("Current/voltage telemetry timeout")
        if not 20 <= self.vbus <= 27.5:
            raise RuntimeError("Bus voltage outside current profile bounds")
        if self.current_exceeded:
            raise RuntimeError("Measured Iq exceeded the 0.5 A profile allowance")
        if self.stalled:
            raise RuntimeError("Sustained low speed; current profile stopped without retry")
        rpm = self.velocity * 60 / GEAR_RATIO
        if rpm < -2 or rpm >= 18:
            raise RuntimeError("Current profile direction/speed bound exceeded")
        if now - self.motion_start >= self.max_motion_seconds or abs(self.position - self.motion_position) / GEAR_RATIO > self.travel_limit:
            raise RuntimeError("Current profile time/travel bound exceeded")
        if now >= self.next_log:
            self.emit("CURRENT_SAMPLE t=%.3f phase=%s target=%.3f rpm=%.3f iq_set=%.5f iq=%.5f iq_mean=%.5f vbus=%.3f ibus=%.5f"
                      % (now - self.motion_start, self.phase, self.command_rpm, rpm,
                         self.iq_setpoint, self.iq, self.iq_mean, self.vbus, self.ibus))
            self.next_log = now + 0.1
        return now

    def phase_stats(self, name, minimum):
        set_sum, measured_sum, count = self.phase_totals[name]
        rpm_sum, rpm_count = self.phase_rpm[name]
        blocks = [{"mean_iq_set": block[3] / block[5], "mean_iq": block[4] / block[5],
                   "samples": block[5]} for block in self.phase_blocks[name]
                  if block[5] >= 20 and block[2] - block[1] >= 0.4]
        if count < minimum or not rpm_count or len(blocks) < (4 if name == "baseline" else 10):
            raise RuntimeError("Insufficient fresh current samples/blocks for " + name)
        return {"samples": count, "mean_iq_set": set_sum / count,
                "mean_iq": measured_sum / count, "mean_rpm": rpm_sum / rpm_count,
                "blocks_500ms": blocks}

    def perform_motion(self):
        self.motion_start = self.clock.monotonic()
        self.motion_position = self.position
        self.iq_window = []
        applied_current = min(self.current_limit, self.original["current_limit"])
        self.emit("CURRENT_CONFIG target_rpm=10 current_limit=%.3f iq_rate_ms=20 average_ms=200" % applied_current)
        self.phase = "ramp"
        self.segment(10.0, 2.0, ramp=True)
        self.phase = "baseline"
        self.phase_started = self.clock.monotonic()
        self.emit("CURRENT_PHASE baseline: keep brake released")
        self.segment(10.0, 2.1)
        baseline = self.phase_stats("baseline", 100)
        if baseline["mean_rpm"] < 8 or abs(baseline["mean_iq_set"]) >= 0.4 or not self.stall_armed:
            raise RuntimeError("Baseline lacks stable speed/current headroom; load window refused")
        self.phase = "load"
        self.phase_started = self.clock.monotonic()
        self.emit("CURRENT_PHASE load: apply light fixture load now")
        self.segment(10.0, self.load_seconds)
        load = self.phase_stats("load", 250)
        self.phase = "ramp"
        self.segment(0.0, 2.0, ramp=True)
        return {"completed": True, "target_rpm": self.target_rpm, "current_limit": applied_current,
                "baseline": baseline, "load": load,
                "baseline_iq_mean": baseline["mean_iq"], "load_iq_mean": load["mean_iq"],
                "load_minus_baseline_iq": load["mean_iq"] - baseline["mean_iq"],
                "load_minus_baseline_iq_set": load["mean_iq_set"] - baseline["mean_iq_set"],
                "baseline_samples": baseline["samples"], "load_samples": load["samples"],
                "max_abs_iq": self.max_iq, "max_vbus": self.max_vbus,
                "output_revolutions": (self.position - self.motion_position) / GEAR_RATIO}
