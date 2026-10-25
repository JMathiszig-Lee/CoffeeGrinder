"""Behavioral simulation checks without hardware or wall-clock waits."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui.demo import DemoMotor


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        raise AssertionError("Simulation must never sleep")


class DemoMotorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.motor = DemoMotor(self.clock)

    def advance(self, seconds, step=0.02):
        deadline = self.clock.now + seconds
        while self.clock.now < deadline - 0.000001:
            self.clock.now = min(deadline, self.clock.now + step)
            self.motor.update()
        return self.motor.snapshot()

    def test_initially_stopped_and_legacy_one_second_slew_is_preserved(self):
        initial = self.motor.snapshot()
        self.assertEqual((initial["state"], initial["measured_rpm"]), ("idle", 0.0))
        self.assertTrue(initial["ready"] and initial["stop_confirmed"])
        self.assertIsNone(initial["load_percent"])
        self.motor.start()
        self.clock.now = 1.0
        self.motor.update()
        self.assertEqual(self.motor.snapshot()["measured_rpm"], 25)
        self.motor.stop()
        self.assertFalse(self.motor.snapshot()["stop_confirmed"])
        self.clock.now = 2.0
        self.motor.update()
        self.assertEqual(self.motor.snapshot()["measured_rpm"], 0)
        self.assertTrue(self.motor.snapshot()["stop_confirmed"])

    def test_reverse_feedback_is_signed_but_command_is_a_magnitude(self):
        self.motor.set_rpm(20)
        self.motor.set_direction(-1)
        self.motor.start()
        status = self.advance(1)
        self.assertEqual(status["measured_rpm"], -20)
        self.assertEqual(status["commanded_rpm"], 20)
        self.assertEqual(status["direction"], -1)
        self.assertTrue(status["supports_reverse"] and status["supports_stall_recovery"])
        self.motor.stop()
        self.assertIsNone(self.motor.snapshot()["load_percent"])
        status = self.advance(1)
        self.assertEqual(status["measured_rpm"], 0)
        self.assertTrue(status["stop_confirmed"])

    def test_direction_and_recovery_configuration_require_stopped_feedback(self):
        self.motor.configure_stall_recovery(True, 0.4)
        self.motor.start()
        self.advance(1)
        with self.assertRaisesRegex(RuntimeError, "Stop"):
            self.motor.set_direction(-1)
        with self.assertRaisesRegex(RuntimeError, "Stop"):
            self.motor.configure_stall_recovery(False, 0.4)
        self.motor.stop()
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(-1)
        self.advance(1)
        self.motor.set_direction(-1)
        self.motor.configure_stall_recovery(False, 0.1)
        self.assertEqual(self.motor.snapshot()["state"], "idle")

    def test_cycle_gives_baseline_then_beans_then_a_qualifying_unloaded_window(self):
        self.motor.start()
        samples = []
        for _ in range(160):
            status = self.advance(0.1)
            samples.append((self.clock.now, status["load_percent"]))
        self.assertTrue(all(16 <= load <= 20 for t, load in samples if 0.1 <= t < 4.4))
        self.assertTrue(all(53 <= load <= 57 for t, load in samples if 4.7 <= t <= 10.3))
        self.assertTrue(all(16 <= load <= 20 for t, load in samples if t >= 10.7))
        # More than the default three-second unloaded delay follows a proven
        # beans load. Only the model may turn that signal into a stop command.
        self.assertGreater(samples[-1][0] - 10.7, 3)
        self.assertEqual(self.motor.snapshot()["state"], "running")

    def test_load_selection_never_starts_and_beans_empty_profiles_are_distinct(self):
        for profile in ("beans", "empty", "cycle", "jam"):
            self.motor.simulate_load(profile)
            status = self.advance(1)
            self.assertEqual(status["state"], "idle")
            self.assertIsNone(status["load_percent"])
        other = DemoMotor(self.clock)
        other.simulate_load("beans")
        other.start()
        self.clock.now += 1
        other.update()
        self.assertTrue(53 <= other.snapshot()["load_percent"] <= 57)
        other.simulate_load("empty")
        self.assertTrue(16 <= other.snapshot()["load_percent"] <= 20)

    def test_jam_requires_observed_reverse_dwell_and_preserves_previous_profile(self):
        self.motor.simulate_load("beans")
        self.motor.start()
        self.advance(5)
        self.motor.simulate_load("jam")
        status = self.advance(1)
        self.assertEqual(status["measured_rpm"], 0)
        self.assertTrue(96 <= status["load_percent"] <= 100)
        self.motor.stop()
        self.advance(3)
        self.assertEqual(self.motor.snapshot()["simulated_load"], "jam")
        self.motor.set_direction(-1)
        self.motor.set_rpm(20)
        self.motor.start()
        self.advance(0.22)
        self.assertLess(self.motor.snapshot()["measured_rpm"], -5)
        self.advance(0.18)
        self.assertEqual(self.motor.snapshot()["simulated_load"], "jam")
        self.advance(0.06)
        self.assertEqual(self.motor.snapshot()["simulated_load"], "beans")
        self.motor.stop()
        self.advance(1)
        self.motor.set_direction(1)
        self.motor.set_rpm(110)
        self.motor.start()
        status = self.advance(2)
        self.assertGreater(status["measured_rpm"], 0)
        self.assertTrue(53 <= status["load_percent"] <= 57)

    def test_manual_stop_cancels_reverse_dwell_and_never_restarts(self):
        self.motor.configure_stall_recovery(True, 0.4)
        self.motor.simulate_load("jam")
        self.motor.set_direction(-1)
        self.motor.start()
        self.advance(0.24)
        self.motor.stop()
        status = self.advance(10)
        self.assertEqual((status["state"], status["measured_rpm"]), ("idle", 0))
        self.assertEqual(status["simulated_load"], "jam")
        self.assertIsNone(status["load_percent"])
        self.motor.close()
        self.advance(10)
        self.assertEqual(self.motor.snapshot()["state"], "closed")
        self.assertFalse(self.motor.snapshot()["ready"])
        with self.assertRaises(RuntimeError):
            self.motor.start()

    def test_minimum_reverse_speed_can_clear_a_simulated_jam(self):
        self.motor.simulate_load("jam")
        self.motor.set_rpm(5)
        self.motor.set_direction(-1)
        self.motor.start()
        status = self.advance(.5)
        self.assertEqual(status["measured_rpm"], -5)
        self.assertNotEqual(status["simulated_load"], "jam")

    def test_invalid_commands_never_start_motion(self):
        for value in (True, -1, 0.1, 4.99, 110.01, float("nan"), float("inf"), "20"):
            with self.subTest(rpm=value), self.assertRaises(ValueError):
                self.motor.set_rpm(value)
        for value in (True, 0, 2, 1.0):
            with self.subTest(direction=value), self.assertRaises(ValueError):
                self.motor.set_direction(value)
        for enabled, seconds in ((1, 0.4), (True, float("nan")), (False, 0), (True, 3.1)):
            with self.assertRaises(ValueError):
                self.motor.configure_stall_recovery(enabled, seconds)
        with self.assertRaises(ValueError):
            self.motor.simulate_load("random")
        self.motor.set_rpm(0)
        with self.assertRaises(RuntimeError):
            self.motor.start()
        self.assertTrue(self.motor.snapshot()["stop_confirmed"])


if __name__ == "__main__":
    unittest.main()
