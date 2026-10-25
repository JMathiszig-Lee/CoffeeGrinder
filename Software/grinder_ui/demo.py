"""Synthetic signed motor feedback for the explicitly selected simulation mode.

The cycle profile supplies baseline, beans, then empty load so the UI can
exercise autostop. A jam is latched until reverse feedback proves movement;
this backend never starts or reverses itself. No hardware is imported.
"""

import math
import time

from .settings import DEFAULT_RPM, MAX_RPM, MIN_RPM


def _number(value, low, high, name):
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value)):
        raise ValueError(name + " must be finite and between %s and %s" % (low, high))
    return float(value)


class DemoMotor:
    """Stopped on creation, with 25 RPM/s acceleration and 35 RPM/s stopping."""

    supports_reverse = True
    supports_stall_recovery = True

    def __init__(self, clock=time):
        self.clock = clock
        self.state = "idle"
        self.target_rpm = float(DEFAULT_RPM)
        self.actual_rpm = 0.0
        self._direction = 1
        self._last_update = clock.monotonic()
        self._profile = "cycle"
        self._cycle_elapsed = 0.0
        self._jammed = False
        self._reverse_since = None
        self._resume_profile = False
        self._recovery_enabled = False
        self._recovery_detection_s = 0.4

    @property
    def direction(self):
        return self._direction

    def _stopped(self):
        return self.state == "idle" and abs(self.actual_rpm) < 1.125

    def set_rpm(self, rpm):
        rpm = _number(rpm, 0, MAX_RPM, "RPM")
        if rpm != 0 and rpm < MIN_RPM:
            raise ValueError("RPM must be zero or between 5 and 110")
        self.update()
        self.target_rpm = rpm
        if not rpm and self.state in ("starting", "running"):
            self.stop()

    def set_direction(self, direction):
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError("Direction must be +1 or -1")
        self.update()
        if not self._stopped():
            raise RuntimeError("Stop the demo motor before changing direction")
        self._direction = direction

    def configure_stall_recovery(self, enabled, detection_s=0.4):
        if type(enabled) is not bool:
            raise ValueError("Recovery enabled must be a bool")
        detection_s = _number(detection_s, 0.1, 3.0, "Stall detection time")
        self.update()
        if not self._stopped():
            raise RuntimeError("Stop the demo motor before configuring recovery")
        self._recovery_enabled, self._recovery_detection_s = enabled, detection_s

    def simulate_load(self, kind):
        """Select a profile or latch a jam, without issuing a motion command.

        Selecting a normal profile during a jam changes what returns after
        reverse clears the jam; it does not remove the jam itself.
        """
        if kind not in ("cycle", "beans", "empty", "jam"):
            raise ValueError("Load profile must be cycle, beans, empty or jam")
        self.update()
        if kind == "jam":
            self._jammed = True
            self._reverse_since = None
        else:
            self._profile = kind
            self._cycle_elapsed = 0.0

    def start(self):
        self.update()
        if not self._stopped() or self.target_rpm <= 0:
            raise RuntimeError("Demo motor is not ready")
        if self.direction == 1:
            if not self._resume_profile:
                self._cycle_elapsed = 0.0
            self._resume_profile = False
        self.state = "starting"

    def stop(self):
        self.update()
        self._reverse_since = None
        if self.state != "closed":
            self.state = "stopping" if abs(self.actual_rpm) > 0 else "idle"

    def update(self):
        now = self.clock.monotonic()
        dt = max(0.0, now - self._last_update)
        self._last_update = now
        if self.state == "closed":
            return
        if self.state == "starting":
            self.state = "running"
        running = self.state == "running"
        jam_forward = running and self.direction == 1 and self._jammed
        wanted = self.direction * self.target_rpm if running and not jam_forward else 0.0
        rate = 140.0 if jam_forward else 25.0 if abs(wanted) > abs(self.actual_rpm) else 35.0
        before = self.actual_rpm
        change = wanted - before
        self.actual_rpm += max(-rate * dt, min(rate * dt, change))
        if self.state == "stopping" and abs(self.actual_rpm) < 0.000001:
            self.actual_rpm = 0.0
            self.state = "idle"
        if running and self.direction == 1 and not jam_forward:
            threshold = 0.8 * self.target_rpm
            if self.actual_rpm >= threshold:
                eligible = dt if before >= threshold else max(0.0, dt - (threshold - before) / rate)
                self._cycle_elapsed += eligible
        if self._jammed and running and self.direction == -1 and self.actual_rpm <= -5:
            if self._reverse_since is None:
                self._reverse_since = now
            elif now - self._reverse_since >= 0.2:
                self._jammed = False
                self._resume_profile = True
                self._reverse_since = None
        else:
            self._reverse_since = None

    def close(self):
        self.actual_rpm = 0.0
        self._reverse_since = None
        self.state = "closed"

    def snapshot(self):
        running = self.state == "running"
        if not running:
            load = None
        else:
            if self._jammed and self.direction == 1:
                baseline = 98.0
            elif self.direction == -1:
                baseline = 18.0
            else:
                beans = self._profile == "beans" or (self._profile == "cycle"
                                                        and 1 <= self._cycle_elapsed < 7)
                baseline = 55.0 if beans else 18.0
            load = max(0.0, min(100.0, baseline + 2 * math.sin(self.clock.monotonic() * 0.8)))
        return {"state": self.state, "ready": self._stopped(), "running": running,
                "target_rpm": self.target_rpm, "commanded_rpm": self.target_rpm,
                "measured_rpm": self.actual_rpm, "direction": self.direction,
                "supports_reverse": True, "supports_stall_recovery": True,
                "stall_recovery_enabled": self._recovery_enabled,
                "stall_detection_s": self._recovery_detection_s,
                "load_percent": load, "load_valid": running, "load_source": "simulation",
                "telemetry_valid": True, "fault": None, "stop_confirmed": self._stopped(),
                "simulated_load": "jam" if self._jammed else self._profile}
