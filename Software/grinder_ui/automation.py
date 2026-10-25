"""Observed-load autostop and bounded, explicitly enabled jam recovery.

The caller owns the update loop and the initial user-requested start. No timer,
thread, hardware import, or backend fault acknowledgement lives here. Manual
STOP must call cancel() before backend.stop(); cancel invalidates every pending
automatic transition, including starts which have not yet been issued.
"""

import math
import time

from .settings import MAX_RPM, MIN_RPM, validate_options


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _target(value):
    if not _finite(value) or not MIN_RPM <= value <= MAX_RPM:
        raise ValueError("Grind RPM must be between 5 and 110")
    return float(value)


class GrindAutomation:
    """Pure, nonblocking supervisor; update after the backend's own update."""

    _RECOVERY = ("stopping_reverse", "reverse", "stopping_resume", "resuming")
    _MAX_SAMPLE_GAP = 0.35
    _STOP_TIMEOUT = 10.0
    _START_TIMEOUT = 2.0

    def __init__(self, backend, clock=time, max_sample_gap_s=0.35):
        if not _finite(max_sample_gap_s) or not 0.05 <= max_sample_gap_s <= 1.0:
            raise ValueError("Sample gap must be between 0.05 and 1 second")
        self.backend = backend
        self.clock = clock
        self._max_sample_gap = float(max_sample_gap_s)
        self._options = validate_options({})
        self._phase = "idle"
        self._message = self._fault = None
        self._active = False
        self._attempts = 0
        self._target = float(MAX_RPM)
        self._deadline = self._remaining = self._last_update = None
        self._forward_started = self._command_since = None
        self._reverse_proven = self._reverse_end = self._reverse_lost = None
        self._reverse_data_seen = False
        self._reset_observations()

    def _reset_observations(self):
        self._speed_since = self._loaded_since = self._unloaded_since = None
        self._stall_since = None
        self._loaded_seen = False
        self._remaining = None

    def _supports(self, name, status=None):
        value = getattr(self.backend, name, None)
        return bool(value if value is not None else (status or {}).get(name, False))

    def prepare(self, target_rpm, options):
        """Configure an idle backend before an explicit user start.

        A local fault requires separate acknowledgement. Configuration errors
        stop the backend and propagate, so a caller cannot proceed to start.
        """
        if self._fault:
            raise RuntimeError("Acknowledge automation fault before starting")
        if self._active:
            raise RuntimeError("Stop automation before preparing another run")
        selected = _target(target_rpm)
        values = validate_options(options)
        self.cancel()
        self._options, self._target = values, selected
        self._attempts = 0
        try:
            status = self.backend.snapshot()
            if self._supports("supports_reverse", status):
                self.backend.set_direction(1)
            configure = getattr(self.backend, "configure_stall_recovery", None)
            if configure is not None:
                configure(values["stall_recovery_enabled"], values["stall_detect_s"])
        except Exception as error:
            self._fail("Automation setup: " + str(error))
            raise RuntimeError(self._fault)
        self._phase = "prepared"

    def begin(self):
        """Arm after the caller has issued its explicit start successfully."""
        if self._fault or self._phase != "prepared":
            raise RuntimeError("Prepare automation before starting")
        now = self.clock.monotonic()
        self._active = True
        self._phase = "grinding"
        self._message = None
        self._deadline = now + self._START_TIMEOUT
        self._last_update = now
        self._forward_started = self._command_since = None
        self._reset_observations()

    def cancel(self):
        """Forget every pending action; the caller separately stops the motor."""
        self._active = False
        self._phase = "fault" if self._fault else "idle"
        self._message = self._fault
        self._deadline = self._last_update = None
        self._forward_started = self._command_since = None
        self._reverse_proven = self._reverse_end = self._reverse_lost = None
        self._reverse_data_seen = False
        self._reset_observations()

    @staticmethod
    def _healthy(status):
        return (status.get("telemetry_valid") is True and not status.get("fault")
                and _finite(status.get("measured_rpm")))

    @classmethod
    def _stationary(cls, status):
        return (cls._healthy(status) and status.get("state") == "idle"
                and status.get("ready") is True and status.get("stop_confirmed") is True
                and not status.get("running") and abs(status["measured_rpm"]) < 1.125)

    @classmethod
    def _running_data(cls, status):
        load = status.get("load_percent")
        return (cls._healthy(status) and status.get("state") == "running"
                and status.get("running") is True and status.get("load_valid") is True
                and _finite(load) and 0 <= load <= 100)

    def clear_fault(self):
        """Acknowledge only our fault, after a fresh healthy confirmed stop."""
        if self._active or not self._stationary(self.backend.snapshot()):
            raise RuntimeError("Fresh healthy stationary IDLE required to acknowledge fault")
        self._fault = None
        self.cancel()

    def _fail(self, message):
        self._fault = str(message)
        self.cancel()
        try:
            self.backend.stop()
        except Exception as error:
            self._fault += "; stop failed: " + str(error)
        self._phase, self._message = "fault", self._fault

    def _stop_phase(self, phase, now, message):
        self.backend.stop()
        self._phase, self._message = phase, message
        self._deadline = now + self._STOP_TIMEOUT
        self._reset_observations()

    def _start_recovery(self, now, status):
        if not self._options["stall_recovery_enabled"]:
            self._fail("STALL: RECOVERY DISABLED")
        elif not self._supports("supports_reverse", status):
            self._fail("STALL: REVERSE UNAVAILABLE")
        elif self._attempts >= self._options["recovery_attempts"]:
            self._fail("STALL: RETRIES USED")
        else:
            self._attempts += 1
            self._stop_phase("stopping_reverse", now, "CLEARING JAM")

    def _reverse_start(self, now):
        self.backend.set_direction(-1)
        self.backend.set_rpm(self._options["reverse_rpm"])
        self.backend.start()
        self._phase = "reverse"
        self._message = "REVERSING %d/%d" % (self._attempts, self._options["recovery_attempts"])
        self._deadline = now + self._START_TIMEOUT
        self._reverse_proven = self._reverse_end = self._reverse_lost = None
        self._reverse_data_seen = False

    def _forward_start(self, now):
        self.backend.set_direction(1)
        self.backend.set_rpm(self._target)
        self.backend.start()
        self._phase, self._message = "resuming", "RESUMING"
        self._deadline = now + self._START_TIMEOUT
        self._forward_started = self._command_since = None

    def _recovery_update(self, status, now):
        if not self._healthy(status):
            self._fail("RECOVERY: FEEDBACK LOST")
            return
        if now >= self._deadline:
            self._fail("RECOVERY: STOP TIMEOUT" if self._phase.startswith("stopping")
                       else "RECOVERY: START TIMEOUT")
            return
        if self._phase == "stopping_reverse":
            if self._stationary(status):
                self._reverse_start(now)
        elif self._phase == "stopping_resume":
            if self._stationary(status):
                self._forward_start(now)
        elif self._phase == "reverse":
            state = status.get("state")
            if state not in ("starting", "running"):
                self._fail("RECOVERY: REVERSE STOPPED")
            elif state == "running":
                if not self._running_data(status):
                    # Closed-loop confirmation precedes the first post-start
                    # Iq frame. It is not yet valid load feedback, so wait
                    # within the start deadline before proving reverse motion.
                    if not self._reverse_data_seen and status.get("load_valid") is False:
                        return
                    self._fail("RECOVERY: LOAD FEEDBACK LOST")
                    return
                self._reverse_data_seen = True
                rpm = status["measured_rpm"]
                if rpm > 2:
                    self._fail("RECOVERY: WRONG DIRECTION")
                    return
                threshold = min(3.0, self._options["reverse_rpm"] * 0.25)
                if self._reverse_proven is None:
                    if rpm <= -threshold:
                        self._reverse_proven = now
                        self._reverse_end = now + self._options["reverse_duration_s"]
                        # Bound the complete reverse, while allowing its chosen duration.
                        self._deadline = self._reverse_end + max(0.25, self._max_sample_gap)
                elif rpm > -threshold:
                    if self._reverse_lost is None:
                        self._reverse_lost = now
                    elif now - self._reverse_lost >= 0.2:
                        self._fail("RECOVERY: REVERSE STALLED")
                        return
                else:
                    self._reverse_lost = None
                if self._reverse_end is not None and now >= self._reverse_end:
                    if self._reverse_lost is not None:
                        self._fail("RECOVERY: REVERSE STALLED")
                    else:
                        self._stop_phase("stopping_resume", now, "CLEARING JAM")
        elif self._phase == "resuming":
            if status.get("state") == "running":
                if not self._running_data(status):
                    if status.get("load_valid") is not False:
                        self._fail("RECOVERY: LOAD FEEDBACK LOST")
                elif status["measured_rpm"] < -2:
                    self._fail("RECOVERY: WRONG DIRECTION")
                else:
                    self._phase, self._message = "grinding", None
                    self._forward_started = now
                    self._command_since = None
                    self._deadline = None
                    self._reset_observations()
            elif status.get("state") != "starting":
                self._fail("RECOVERY: FORWARD START STOPPED")

    def _grind_update(self, status, now):
        state = status.get("state")
        if self._forward_started is None:
            if self._deadline is not None and now >= self._deadline:
                self._fail("START: TIMEOUT")
                return
            if state == "starting":
                return
            if state == "running":
                self._forward_started = now
                self._deadline = None
        if state != "running" or not status.get("running"):
            self.cancel()
            return
        if not self._running_data(status):
            self._reset_observations()
            self._command_since = None
            self._message = "WAITING FOR FEEDBACK"
            return
        self._message = None
        rpm, load = status["measured_rpm"], status["load_percent"]
        if rpm < -2 or status.get("direction", 1) != 1:
            self._fail("GRIND: WRONG DIRECTION")
            return
        commanded = status.get("commanded_rpm", self._target)
        if _finite(commanded) and commanded >= min(5.0, self._target):
            if self._command_since is None:
                self._command_since = now
        else:
            self._command_since = self._stall_since = None
        eligible = (self._command_since is not None and now - self._command_since >= 0.5
                    and now - self._forward_started >= 0.5)
        # The displayed load is low-pass filtered. Stall protection uses the
        # fresh unfiltered demand when available, so that display filtering
        # does not consume the controller's bounded hard-stall safety margin.
        # Generic/demo backends can supply their load percentage alone.
        demand, limit = status.get("iq_setpoint_a"), status.get("current_limit_a")
        stall_load = (min(100.0, abs(demand) / limit * 100.0)
                      if _finite(demand) and _finite(limit) and limit > 0 else load)
        stalled = (eligible and rpm < min(3.0, self._target * 0.25)
                   and stall_load >= self._options["stall_percent"])
        if stalled:
            if self._stall_since is None:
                self._stall_since = now
            elif now - self._stall_since >= self._options["stall_detect_s"]:
                self._start_recovery(now, status)
                return
        else:
            self._stall_since = None
        if not self._options["autostop_enabled"]:
            return
        settled_command = (_finite(commanded)
                           and abs(commanded - self._target) <= max(0.5, self._target * 0.05))
        if not settled_command or not 0.8 * self._target <= rpm <= 1.2 * self._target:
            self._speed_since = self._loaded_since = self._unloaded_since = None
            self._remaining = None
            return
        if self._speed_since is None:
            self._speed_since = now
        if now - self._speed_since < 0.5:
            return
        low = self._options["unload_percent"]
        high = low + self._options["load_hysteresis"]
        if load >= high:
            self._unloaded_since = self._remaining = None
            if self._loaded_since is None:
                self._loaded_since = now
            elif now - self._loaded_since >= 0.3:
                self._loaded_seen = True
        else:
            self._loaded_since = None
            if not self._loaded_seen or load > low:
                self._unloaded_since = self._remaining = None
                return
            if self._unloaded_since is None:
                self._unloaded_since = now
            self._remaining = max(0.0, self._options["autostop_delay_s"]
                                  - (now - self._unloaded_since))
            self._message = "AUTOSTOP %.1fs" % self._remaining
            if self._remaining <= 0:
                self.backend.stop()
                self.cancel()
                self._phase, self._message = "complete", "Beans complete"

    def update(self, status, target_rpm):
        if not self._active:
            return
        now = self.clock.monotonic()
        try:
            selected = _target(target_rpm)
            if selected != self._target:
                self._target = selected
                self._reset_observations()
            if status.get("fault") or status.get("state") == "fault":
                self._fail("MOTOR: " + str(status.get("fault") or "fault"))
                return
            gap = now - self._last_update if self._last_update is not None else 0
            self._last_update = now
            if gap < 0:
                self._fail("AUTOMATION: CLOCK MOVED BACKWARD")
                return
            recovering = self._phase in self._RECOVERY
            if gap > self._max_sample_gap:
                if recovering:
                    self._fail("RECOVERY: UPDATE TIMEOUT")
                    return
                self._reset_observations()
                self._command_since = None
            if recovering:
                self._recovery_update(status, now)
            else:
                self._grind_update(status, now)
        except Exception as error:
            self._fail("AUTOMATION: " + str(error))

    def snapshot(self):
        return {"phase": self._phase, "message": self._message, "fault": self._fault,
                "remaining_s": self._remaining, "attempts": self._attempts,
                "active": self._active, "recovering": self._phase in self._RECOVERY}
