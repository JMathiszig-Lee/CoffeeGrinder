"""Timed observations and delayed motor transitions for grind automation."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui.automation import GrindAutomation
from grinder_ui.settings import DEFAULT_OPTIONS
from grinder_api import GrinderController
from test_controller import Drive


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class TimedMotor:
    """Backend transitions run on their own clock, never on supervisor calls."""

    supports_reverse = True
    supports_stall_recovery = True

    def __init__(self, clock):
        self.clock = clock
        self.state = "idle"
        self.direction = 1
        self.target = 20.0
        self.rpm = 0.0
        self.load = 55.0
        self.valid = self.load_valid = True
        self.fault = None
        self.jammed = self.reverse_jammed = False
        self.clear_on_reverse = True
        self.hold_stop = False
        self.calls = []
        self._last = self._start_at = clock.now

    def snapshot(self):
        idle = self.state == "idle" and abs(self.rpm) < 1.125
        running = self.state == "running"
        load = 98.0 if self.jammed and self.direction == 1 else self.load
        return {"state": self.state, "ready": idle, "stop_confirmed": idle,
                "running": running, "telemetry_valid": self.valid,
                "load_valid": running and self.load_valid,
                "load_percent": load if running and self.load_valid else None,
                "measured_rpm": self.rpm if self.valid else None,
                "commanded_rpm": self.target, "direction": self.direction,
                "fault": self.fault, "supports_reverse": self.supports_reverse}

    def _idle(self):
        status = self.snapshot()
        if not (status["ready"] and status["stop_confirmed"] and self.valid and not self.fault):
            raise RuntimeError("not confirmed healthy idle")

    def set_direction(self, direction):
        self._idle()
        self.direction = direction
        self.calls.append(("direction", direction))

    def configure_stall_recovery(self, enabled, detection_s):
        self._idle()
        self.calls.append(("configure", enabled, detection_s))

    def set_rpm(self, rpm):
        self.target = float(rpm)
        self.calls.append(("rpm", rpm))

    def start(self):
        self._idle()
        self.state = "starting"
        self._start_at = self.clock.now
        self.calls.append(("start", self.direction, self.target))

    def stop(self):
        self.calls.append(("stop",))
        self.state = "stopping"

    def update(self):
        dt = self.clock.now - self._last
        self._last = self.clock.now
        if self.state == "starting" and self.clock.now - self._start_at >= 0.1:
            self.state = "running"
        if self.state == "running":
            jammed = self.jammed if self.direction == 1 else self.reverse_jammed
            wanted = 0 if jammed else self.direction * self.target
            step = 100 * dt if jammed else 25 * dt
        else:
            wanted = 0
            step = 0 if self.hold_stop else 35 * dt
        self.rpm += max(-step, min(step, wanted - self.rpm))
        if self.state == "stopping" and abs(self.rpm) < 0.000001 and not self.hold_stop:
            self.rpm = 0.0
            self.state = "idle"
        if self.direction == -1 and self.rpm < -5 and self.clear_on_reverse:
            self.jammed = False


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.motor = TimedMotor(self.clock)
        self.auto = GrindAutomation(self.motor, self.clock)
        self.selected = 20

    def run_for(self, seconds, step=0.05):
        end = self.clock.now + seconds
        while self.clock.now < end - 0.0000001:
            self.clock.now = min(end, self.clock.now + step)
            self.motor.update()
            self.auto.update(self.motor.snapshot(), self.selected)

    def until(self, predicate, timeout=12):
        deadline = self.clock.now + timeout
        while not predicate() and self.clock.now < deadline:
            self.run_for(0.05)
        self.assertTrue(predicate(), self.auto.snapshot())

    def begin(self, **changes):
        options = dict(DEFAULT_OPTIONS)
        options.update(changes)
        self.auto.prepare(self.selected, options)
        self.motor.set_rpm(self.selected)
        self.motor.start()
        self.auto.begin()

    def settle(self, **changes):
        self.begin(**changes)
        self.run_for(2)

    def phase(self):
        return self.auto.snapshot()["phase"]

    def starts(self):
        return [call for call in self.motor.calls if call[0] == "start"]

    def test_prepare_configures_but_never_starts_and_options_are_copied(self):
        options = dict(DEFAULT_OPTIONS)
        self.motor.direction = -1
        self.auto.prepare(20, options)
        self.assertEqual(self.motor.calls, [("direction", 1), ("configure", False, 0.4)])
        self.assertFalse(self.auto.snapshot()["active"])
        options["stall_recovery_enabled"] = True
        self.assertFalse(self.auto._options["stall_recovery_enabled"])
        with self.assertRaises(RuntimeError):
            GrindAutomation(self.motor, self.clock).begin()

    def test_no_beans_startup_never_autostops(self):
        self.motor.load = 10
        self.settle(autostop_enabled=True, autostop_delay_s=0.5)
        self.run_for(10)
        self.assertTrue(self.auto.snapshot()["active"])
        self.assertIsNone(self.auto.snapshot()["remaining_s"])
        self.assertFalse(any(call[0] == "stop" for call in self.motor.calls))

    def test_loaded_qualification_requires_established_speed_and_sustained_load(self):
        self.begin(autostop_enabled=True, autostop_delay_s=0.5)
        self.run_for(0.8)
        self.motor.load = 10
        self.run_for(2)
        self.assertTrue(self.auto.snapshot()["active"])
        self.motor.load = 55
        self.run_for(0.2)
        self.motor.load = 10
        self.run_for(1)
        self.assertTrue(self.auto.snapshot()["active"])
        self.motor.load = 55
        self.run_for(0.45)
        self.motor.load = 10
        self.run_for(0.3)
        self.assertIsNotNone(self.auto.snapshot()["remaining_s"])
        self.run_for(0.3)
        self.assertEqual(self.phase(), "complete")
        self.assertEqual(self.auto.snapshot()["message"], "Beans complete")
        self.assertEqual(len(self.starts()), 1)
        self.run_for(5)
        self.assertEqual(len(self.starts()), 1)

    def test_unload_countdown_requires_continuous_low_and_restarts_after_hysteresis(self):
        self.settle(autostop_enabled=True, autostop_delay_s=0.5)
        self.motor.load = 10
        self.run_for(0.35)
        self.motor.load = 29  # In the hysteresis band: not continuously unloaded.
        self.run_for(0.1)
        self.assertIsNone(self.auto.snapshot()["remaining_s"])
        self.motor.load = 10
        self.run_for(0.35)
        self.assertTrue(self.auto.snapshot()["active"])
        self.motor.load = 55
        self.run_for(0.1)
        self.assertIsNone(self.auto.snapshot()["remaining_s"])
        self.motor.load = 10
        self.run_for(0.6)
        self.assertEqual(self.phase(), "complete")

    def test_target_change_and_invalid_feedback_remove_prior_load_qualification(self):
        for invalid in ("target", "telemetry", "load", "nan"):
            with self.subTest(invalid=invalid):
                self.setUp()
                self.settle(autostop_enabled=True, autostop_delay_s=0.5)
                self.motor.load = 10
                self.run_for(0.2)
                if invalid == "target":
                    self.selected = 25
                    self.motor.set_rpm(25)
                elif invalid == "telemetry":
                    self.motor.valid = False
                elif invalid == "load":
                    self.motor.load_valid = False
                else:
                    self.motor.load = float("nan")
                self.run_for(0.1)
                self.motor.valid = self.motor.load_valid = True
                self.motor.load = 10
                self.run_for(2)
                self.assertTrue(self.auto.snapshot()["active"])
                self.assertIsNone(self.auto.snapshot()["remaining_s"])

    def test_large_observation_gap_cannot_complete_countdown(self):
        self.settle(autostop_enabled=True, autostop_delay_s=0.5)
        self.motor.load = 10
        self.run_for(0.2)
        self.run_for(1, step=1)
        self.run_for(1)
        self.assertTrue(self.auto.snapshot()["active"])
        self.assertIsNone(self.auto.snapshot()["remaining_s"])

    def test_downward_target_transient_cannot_qualify_a_new_loaded_cycle(self):
        self.selected = 110
        self.begin(autostop_enabled=True, autostop_delay_s=0.5)
        self.run_for(6)
        self.selected = 10
        self.motor.set_rpm(10)
        self.run_for(1)  # Still decelerating far above the new selected speed.
        self.motor.load = 10
        self.run_for(5)
        self.assertTrue(self.auto.snapshot()["active"])
        self.assertIsNone(self.auto.snapshot()["remaining_s"])

    def test_command_ramp_must_finish_before_load_can_arm_autostop(self):
        self.begin(autostop_enabled=True, autostop_delay_s=0.5)
        for _ in range(30):
            self.clock.now += 0.05
            status = self.motor.snapshot()
            status.update(state="running", running=True, measured_rpm=20,
                          load_valid=True, load_percent=55, commanded_rpm=8)
            self.auto.update(status, 20)
        self.motor.load = 10
        self.run_for(3)
        self.assertTrue(self.auto.snapshot()["active"])
        self.assertIsNone(self.auto.snapshot()["remaining_s"])

    def test_startup_jam_is_detected_without_waiting_for_actual_target_speed(self):
        self.selected = 110
        self.motor.jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "stopping_reverse", timeout=1.5)
        self.assertEqual(self.auto.snapshot()["attempts"], 1)
        self.assertEqual(self.starts(), [("start", 1, 110.0)])
        self.assertEqual(sum(call[0] == "stop" for call in self.motor.calls), 1)

    def test_brief_stall_and_low_load_low_speed_do_not_trigger_recovery(self):
        self.settle(stall_recovery_enabled=True)
        self.motor.jammed = True
        self.run_for(0.4)  # Deceleration plus less than the full detection interval.
        self.motor.jammed = False
        self.motor.load = 10
        self.run_for(1)
        self.assertEqual(self.auto.snapshot()["attempts"], 0)
        self.assertEqual(len(self.starts()), 1)

    def test_stall_uses_fresh_raw_demand_while_autostop_keeps_filtered_load(self):
        self.settle(stall_recovery_enabled=True)
        self.motor.load = 10
        for _ in range(10):
            self.clock.now += 0.05
            status = self.motor.snapshot()
            status.update(measured_rpm=0, iq_setpoint_a=-0.49, current_limit_a=0.5)
            self.auto.update(status, self.selected)
        self.assertEqual(self.phase(), "stopping_reverse")
        self.assertEqual(self.auto.snapshot()["attempts"], 1)

    def test_invalid_raw_demand_or_limit_uses_generic_load_without_dividing(self):
        for demand, limit in ((float("nan"), 0.5), (0.49, 0), (None, 0.5),
                              (0.49, float("inf"))):
            with self.subTest(demand=demand, limit=limit):
                self.setUp()
                self.settle(stall_recovery_enabled=True)
                self.motor.load = 10
                for _ in range(12):
                    self.clock.now += 0.05
                    status = self.motor.snapshot()
                    status.update(measured_rpm=0, iq_setpoint_a=demand, current_limit_a=limit)
                    self.auto.update(status, self.selected)
                self.assertEqual(self.auto.snapshot()["attempts"], 0)
                self.assertIsNone(self.auto.snapshot()["fault"])

    def test_recovery_requires_stationary_confirmation_then_signed_reverse_and_latest_target(self):
        self.settle(stall_recovery_enabled=True)
        self.motor.jammed = True
        self.until(lambda: self.phase() == "reverse")
        self.assertTrue(self.auto.snapshot()["recovering"])
        self.assertEqual(self.starts()[-1], ("start", -1, 20.0))
        self.selected = 70
        self.until(lambda: self.phase() == "stopping_resume")
        self.assertLess(self.motor.rpm, -3)
        self.until(lambda: self.phase() == "resuming")
        self.assertEqual(self.starts()[-1], ("start", 1, 70.0))
        self.until(lambda: self.phase() == "grinding")
        self.assertFalse(self.auto.snapshot()["recovering"])
        self.assertEqual(self.auto.snapshot()["attempts"], 1)
        self.assertIsNone(self.auto.snapshot()["fault"])

    def test_manual_cancel_in_every_recovery_phase_prevents_all_later_starts(self):
        for phase in ("stopping_reverse", "reverse", "stopping_resume", "resuming"):
            with self.subTest(phase=phase):
                self.setUp()
                self.settle(stall_recovery_enabled=True)
                self.motor.jammed = True
                self.until(lambda: self.phase() == phase)
                self.auto.cancel()
                self.motor.stop()
                starts = list(self.starts())
                self.run_for(12)
                self.assertEqual(self.starts(), starts)
                self.assertEqual(self.phase(), "idle")
                self.assertFalse(self.auto.snapshot()["active"])

    def test_dropout_during_every_recovery_phase_faults_and_cannot_restart(self):
        for phase in ("stopping_reverse", "reverse", "stopping_resume", "resuming"):
            with self.subTest(phase=phase):
                self.setUp()
                self.settle(stall_recovery_enabled=True)
                self.motor.jammed = True
                self.until(lambda: self.phase() == phase)
                starts = list(self.starts())
                self.motor.valid = False
                self.run_for(0.05)
                self.assertIn("FEEDBACK LOST", self.auto.snapshot()["fault"])
                self.motor.valid = True
                self.run_for(10)
                self.assertEqual(self.starts(), starts)

    def test_attempt_limit_is_per_explicit_run_not_per_successful_reverse(self):
        self.motor.jammed = True
        self.motor.clear_on_reverse = False
        self.begin(stall_recovery_enabled=True, recovery_attempts=2)
        self.until(lambda: self.phase() == "fault", timeout=20)
        self.assertEqual(self.auto.snapshot()["fault"], "STALL: RETRIES USED")
        self.assertEqual(self.auto.snapshot()["attempts"], 2)
        self.assertEqual([call[1] for call in self.starts()], [1, -1, 1, -1, 1])
        self.auto.cancel()
        with self.assertRaises(RuntimeError):
            self.auto.prepare(20, dict(DEFAULT_OPTIONS))
        self.run_for(1)
        self.auto.clear_fault()
        self.auto.prepare(20, dict(DEFAULT_OPTIONS))
        self.assertEqual(self.auto.snapshot()["attempts"], 0)

    def test_disabled_or_unsupported_recovery_stops_with_explanation(self):
        for enabled, supported, message in ((False, True, "RECOVERY DISABLED"),
                                             (True, False, "REVERSE UNAVAILABLE")):
            with self.subTest(enabled=enabled, supported=supported):
                self.setUp()
                self.motor.supports_reverse = supported
                self.motor.jammed = True
                self.begin(stall_recovery_enabled=enabled)
                self.until(lambda: self.phase() == "fault")
                self.assertIn(message, self.auto.snapshot()["fault"])
                self.assertEqual(len(self.starts()), 1)

    def test_reverse_jam_times_out_instead_of_resuming(self):
        self.motor.jammed = self.motor.reverse_jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "fault")
        self.assertIn("START TIMEOUT", self.auto.snapshot()["fault"])
        self.assertEqual([call[1] for call in self.starts()], [1, -1])

    def test_recovery_waits_for_first_poststart_current_then_faults_on_later_loss(self):
        self.motor.jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "reverse")
        self.motor.load_valid = False
        self.run_for(0.3)
        self.assertEqual(self.phase(), "reverse")
        self.assertIsNone(self.auto.snapshot()["fault"])
        self.assertIsNone(self.auto._reverse_proven)
        self.motor.load_valid = True
        self.run_for(0.05)
        self.assertIsNotNone(self.auto._reverse_proven)
        self.motor.load_valid = False
        self.run_for(0.05)
        self.assertIn("LOAD FEEDBACK LOST", self.auto.snapshot()["fault"])

    def test_resume_waits_for_first_poststart_current_with_bounded_deadline(self):
        self.motor.jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "resuming")
        self.motor.load_valid = False
        self.run_for(0.3)
        self.assertEqual(self.phase(), "resuming")
        self.assertIsNone(self.auto.snapshot()["fault"])
        self.motor.load_valid = True
        self.run_for(0.05)
        self.assertEqual(self.phase(), "grinding")

    def test_missing_first_current_sample_cannot_extend_reverse_start_deadline(self):
        self.motor.jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "reverse")
        self.motor.load_valid = False
        self.run_for(2.1)
        self.assertIn("START TIMEOUT", self.auto.snapshot()["fault"])
        self.assertEqual([call[1] for call in self.starts()], [1, -1])

    def test_stop_timeout_is_wall_clock_bounded(self):
        self.settle(stall_recovery_enabled=True)
        self.motor.jammed = True
        self.until(lambda: self.phase() == "stopping_reverse")
        self.motor.state = "stopping"
        self.motor.rpm = 4
        self.motor.hold_stop = True
        self.run_for(10.1)
        self.assertIn("STOP TIMEOUT", self.auto.snapshot()["fault"])
        self.assertEqual(len(self.starts()), 1)

    def test_recovery_update_gap_faults_before_any_next_start(self):
        self.motor.jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "stopping_reverse")
        self.run_for(0.5, step=0.5)
        self.assertIn("UPDATE TIMEOUT", self.auto.snapshot()["fault"])
        self.assertEqual(len(self.starts()), 1)

    def test_simulation_gap_can_tolerate_menu_redraws_without_changing_default_guard(self):
        self.auto = GrindAutomation(self.motor, self.clock, max_sample_gap_s=.75)
        self.motor.jammed = True
        self.begin(stall_recovery_enabled=True)
        self.until(lambda: self.phase() == "reverse")
        for _ in range(4):
            self.run_for(.4, step=.4)
            self.assertIsNone(self.auto.snapshot()["fault"])
        self.run_for(2)
        self.assertEqual(self.phase(), "grinding")

    def test_invalid_sampling_gaps_are_rejected(self):
        for gap in (True, 0, 1.1, float("nan"), float("inf"), "1"):
            with self.subTest(gap=gap), self.assertRaises(ValueError):
                GrindAutomation(self.motor, self.clock, max_sample_gap_s=gap)

    def test_backend_fault_is_never_acknowledged_and_local_ack_requires_healthy_stop(self):
        self.settle()
        self.motor.fault = "drive error"
        self.run_for(0.05)
        self.assertEqual(self.auto.snapshot()["fault"], "MOTOR: drive error")
        self.auto.cancel()
        self.run_for(1)
        with self.assertRaises(RuntimeError):
            self.auto.clear_fault()
        self.motor.fault = None
        self.motor.valid = False
        with self.assertRaises(RuntimeError):
            self.auto.clear_fault()
        self.motor.valid = True
        self.auto.clear_fault()
        self.assertIsNone(self.auto.snapshot()["fault"])
        self.assertEqual(len(self.starts()), 1)

    def test_external_stop_cancels_observation_without_restart(self):
        self.settle(autostop_enabled=True, stall_recovery_enabled=True)
        self.motor.stop()
        self.run_for(3)
        self.assertFalse(self.auto.snapshot()["active"])
        self.assertIsNone(self.auto.snapshot()["fault"])
        self.assertEqual(len(self.starts()), 1)

    def test_clear_fault_does_not_acknowledge_active_motion(self):
        self.settle()
        with self.assertRaises(RuntimeError):
            self.auto.clear_fault()
        self.assertTrue(self.auto.snapshot()["active"])


class ControllerAutomationIntegrationTests(unittest.TestCase):
    """Use the production controller and independent timed CAN drive peer."""

    def setUp(self):
        self.clock = Clock()
        self.drive = Drive(self.clock)
        self.drive.iq = (-0.05, -0.05)
        self.motor = GrinderController(self.drive, clock=self.clock)
        self.auto = GrindAutomation(self.motor, self.clock)
        self.motor.connect()
        self.until(lambda: self.motor.snapshot()["ready"], supervise=False)
        self.auto.prepare(50, {"stall_recovery_enabled": True})
        self.motor.set_rpm(50)
        self.motor.start()
        self.auto.begin()
        self.until(lambda: (self.motor.snapshot()["measured_rpm"] or 0) >= 49.9)
        self.drive.stall = True
        self.drive.rps = 0
        self.drive.iq = (-0.49, -0.49)

    def step(self, supervise=True):
        self.clock.now += 0.005
        self.motor.update()
        if supervise:
            self.auto.update(self.motor.snapshot(), 50)
            if self.auto.snapshot()["phase"] == "stopping_reverse":
                self.drive.stall = False

    def until(self, predicate, timeout=8, supervise=True):
        deadline = self.clock.now + timeout
        while not predicate() and self.clock.now < deadline:
            self.step(supervise)
        self.assertTrue(predicate(), (self.auto.snapshot(), self.motor.snapshot()))

    def test_low_baseline_jam_recovers_before_hard_stall_fallback(self):
        jam_at = self.clock.now
        self.until(lambda: self.auto.snapshot()["phase"] == "stopping_reverse")
        self.assertLess(self.clock.now - jam_at, 0.7)
        self.until(lambda: self.auto.snapshot()["phase"] == "reverse")
        self.until(lambda: self.auto.snapshot()["phase"] == "resuming")
        # Real running state can precede load_valid by one Iq sample.
        self.until(lambda: self.auto.snapshot()["phase"] == "grinding")
        self.assertEqual(self.auto.snapshot()["attempts"], 1)
        self.assertIsNone(self.auto.snapshot()["fault"])
        self.assertIsNone(self.motor.snapshot()["fault"])

    def test_production_controller_manual_stop_cancels_every_recovery_phase(self):
        for phase in ("stopping_reverse", "reverse", "stopping_resume", "resuming"):
            with self.subTest(phase=phase):
                if phase != "stopping_reverse":
                    self.setUp()
                self.until(lambda: self.auto.snapshot()["phase"] == phase)
                self.auto.cancel()
                self.motor.stop()
                sent_before_stop = len(self.drive.attempts)
                for _ in range(600):
                    self.step()
                later_arms = [data for _, command, data in self.drive.attempts[sent_before_stop:]
                              if command == 7 and data[0] == 8]
                self.assertEqual(later_arms, [])
                self.assertEqual(self.auto.snapshot()["phase"], "idle")
                self.assertTrue(self.motor.snapshot()["stop_confirmed"])
                self.assertIsNone(self.motor.snapshot()["fault"])


if __name__ == "__main__":
    unittest.main()
