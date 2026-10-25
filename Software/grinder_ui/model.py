"""Hardware-free UI state, with explicit offline and simulated motor modes."""

import math
import time

from .settings import DEFAULT_RPM, MAX_RPM, MIN_RPM, MemorySettings, SettingsError, validate_rpm
from .automation import GrindAutomation
from .demo import DemoMotor
from .menu import SettingsMenu


def _rpm(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("RPM must be finite")
    if value != 0 and not MIN_RPM <= value <= MAX_RPM:
        raise ValueError("RPM must be zero or between 5 and 110")
    return float(value)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class OfflineMotor:
    """A disconnected placeholder; no telemetry is fabricated."""

    def set_rpm(self, rpm):
        _rpm(rpm)

    def start(self):
        raise RuntimeError("Motor offline")

    def stop(self):
        pass

    def update(self):
        pass

    def close(self):
        pass

    def snapshot(self):
        return {"state": "offline", "ready": False, "running": False,
                "measured_rpm": None, "load_percent": None, "load_valid": False,
                "telemetry_valid": False, "fault": None, "stop_confirmed": False}


class UiModel:
    def __init__(self, backend=None, settings=None, clock=time):
        self.clock = clock
        self.backend = OfflineMotor() if backend is None else backend
        self.settings = MemorySettings() if settings is None else settings
        self.mode = ("offline" if isinstance(self.backend, OfflineMotor) else
                     "simulation" if isinstance(self.backend, DemoMotor) else "live")
        self.default_rpm = validate_rpm(self.settings.default_rpm)
        self.set_rpm = self.default_rpm
        self.options = self.settings.values
        self.automation = self._new_automation()
        self.menu = SettingsMenu(self)
        self._notice = self.settings.message
        self._input_error = self._backend_error = None

    def _new_automation(self):
        # Synthetic feedback tolerates LCD redraws. Live supervision retains
        # the tighter gap and the CAN controller's separate 100 ms deadline.
        return GrindAutomation(self.backend, clock=self.clock,
                               max_sample_gap_s=0.75 if self.mode == "simulation" else 0.35)

    def _failed(self, error):
        self._backend_error = str(error) or "Motor backend failed"
        self.automation.cancel()
        try:
            self.backend.stop()
        except Exception:
            pass

    def _snapshot(self):
        try:
            return self.backend.snapshot()
        except Exception as error:
            self._failed(error)
            return {"state": "fault", "fault": self._backend_error}

    def _select(self, rpm):
        self.set_rpm = max(MIN_RPM, min(MAX_RPM, rpm))
        if (self.mode != "offline" and self._input_error is None and self._backend_error is None
                and self.automation.snapshot()["phase"] not in
                ("stopping_reverse", "reverse", "stopping_resume")):
            try:
                self.backend.set_rpm(self.set_rpm)
            except Exception as error:
                self._failed(error)

    def turn(self, delta):
        if type(delta) is not int:
            raise ValueError("Encoder delta must be an integer")
        if delta:
            self._notice = None
            self._select(self.set_rpm + delta)

    def press(self):
        if self.mode == "offline":
            self._notice = "Motor offline; MENU / DEMO"
            return False
        if self._input_error or self._backend_error or self.automation.snapshot()["fault"]:
            self._notice = "Input or motor fault; start unavailable"
            return False
        status = self._snapshot()
        if (status.get("state") in ("starting", "running", "stopping") or status.get("running")
                or self.automation.snapshot()["active"]):
            self.stop()
            return False
        if status.get("fault") or not status.get("ready", False):
            self._notice = "Motor is not ready"
            return False
        try:
            self.automation.prepare(self.set_rpm, self.options)
            self.backend.set_rpm(self.set_rpm)
            self.backend.start()
            self.automation.begin()
            self._notice = None
            return True
        except Exception as error:
            self._failed(error)
            return False

    def toggle_demo(self):
        if self.mode == "live":
            self._notice = "Disconnect live motor before selecting demo"
            return False
        self.stop()
        if self.mode == "offline":
            self.backend = DemoMotor(self.clock)
            self.backend.set_rpm(self.set_rpm)
            self.mode = "simulation"
            self._notice = "DEMO: simulated motor; press encoder to start"
        else:
            self.backend = OfflineMotor()
            self.mode = "offline"
            self._notice = "Motor offline"
        self._backend_error = None
        self.automation = self._new_automation()
        return True

    def stop(self):
        self.automation.cancel()
        try:
            self.backend.stop()
        except Exception as error:
            self._failed(error)

    def recall_default(self):
        self._select(self.default_rpm)
        self._notice = "Default: %d RPM" % self.default_rpm

    def save_default(self):
        menu_rpm_was_default = self.menu.rpm == self.default_rpm
        try:
            changed = self.settings.save(self.set_rpm)
            verified = validate_rpm(self.settings.default_rpm)
            if verified != self.set_rpm:
                raise SettingsError("Saved default readback did not match")
        except Exception as error:
            self._notice = str(error) or "Default was not saved"
            return False
        self.default_rpm = verified
        if menu_rpm_was_default:
            self.menu.rpm = verified
        self._notice = ("Saved default: %d RPM" if changed else "Default unchanged: %d RPM") % verified
        return True

    def can_edit_settings(self):
        state = self._snapshot()
        if self.automation.snapshot()["active"]:
            return False
        if state.get("state") == "offline":
            return True
        rpm = state.get("measured_rpm")
        return (state.get("state") in ("idle", "fault")
                and state.get("telemetry_valid") and _finite(rpm) and abs(rpm) < 1.125
                and (state.get("ready") or state.get("stop_confirmed")))

    def save_menu(self):
        if self.menu.editing:
            self._notice = "Press encoder to accept edit first"
            return False
        if not self.can_edit_settings():
            self._notice = "Stop motor before saving settings"
            return False
        try:
            self.settings.save_options(self.menu.options)
            self.settings.save(self.menu.rpm)
        except Exception as error:
            self.default_rpm = self.settings.default_rpm
            self.options = self.settings.values
            self._notice = "Settings not fully saved: " + str(error)
            return False
        self.default_rpm = self.settings.default_rpm
        self.options = self.settings.values
        self._notice = "Settings saved"
        return True

    def acknowledge_automation(self):
        if not self.automation.snapshot()["fault"]:
            self._notice = "No automation fault to acknowledge"
            return False
        try:
            self.automation.clear_fault()
            self._notice = "Automation fault acknowledged"
            return True
        except Exception as error:
            self._notice = str(error)
            return False

    def demo_action(self, action):
        if action == "run_demo":
            if self.mode == "offline":
                self.toggle_demo()
            if self.mode != "simulation":
                self._notice = "Demo unavailable with live motor"
                return False
            status = self._snapshot()
            if status.get("ready") and self.press():
                self.menu.page = None
                return True
            self._notice = "Stop demo before starting a new run"
        elif action == "exit_demo":
            if self.mode == "simulation":
                self.toggle_demo()
                self.menu.page = None
                return True
            self._notice = "Demo is not active"
        elif self.mode == "simulation":
            self.backend.simulate_load(action)
            self._notice = "Demo: " + {"beans": "beans added", "empty": "hopper empty",
                                       "jam": "jam applied"}[action]
            self.menu.page = None
            return True
        else:
            self._notice = "Run demo before simulating a load"
        return False

    def show_notice(self, message):
        self._notice = str(message)

    def set_input_error(self, message):
        if self._input_error is None:
            self._input_error = str(message) if message else "Encoder input unavailable"
            self.stop()

    def update(self):
        try:
            self.backend.update()
            if not self._input_error and not self._backend_error:
                self.automation.update(self._snapshot(), self.set_rpm)
        except Exception as error:
            self._failed(error)

    def view(self):
        status = self._snapshot()
        state = status.get("state", "fault")
        automation = self.automation.snapshot()
        fault = status.get("fault") or self._backend_error or automation["fault"]
        if state == "fault" and not fault:
            fault = "Motor fault"
        healthy = (self.mode != "offline" and status.get("telemetry_valid", False)
                   and not fault and not self._input_error)
        actual = status.get("measured_rpm")
        actual = float(actual) if healthy and _finite(actual) else None
        load = status.get("load_percent")
        load = (max(0.0, min(100.0, float(load)))
                if healthy and status.get("running", False) and status.get("load_valid", False)
                and _finite(load) else None)
        return {"mode": self.mode, "state": state, "set_rpm": self.set_rpm,
                "default_rpm": self.default_rpm, "actual_rpm": actual, "load_percent": load,
                "fault": str(fault) if fault else None, "notice": self._notice,
                "input_error": self._input_error, "automation": automation,
                "options": dict(self.options), "menu": self.menu.view()}
