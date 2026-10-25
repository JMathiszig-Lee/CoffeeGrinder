"""Direction and recovery fallback behavior against the timed CAN drive peer."""

from pathlib import Path
import json
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from grinder_api import GrinderController
from test_controller import Clock, Drive


class ControllerDirectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.drive = Drive(self.clock)
        self.motor = GrinderController(self.drive, clock=self.clock)

    def step(self, seconds=0.005):
        self.clock.now += seconds
        self.motor.update()

    def advance(self, seconds):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.step()

    def until(self, predicate, seconds=3):
        end = self.clock.now + seconds
        while self.clock.now < end and not predicate():
            self.step()
        self.assertTrue(predicate(), self.motor.snapshot())

    def ready(self):
        self.motor.connect()
        self.until(lambda: self.motor.snapshot()["ready"])

    def run_ready_motor(self, rpm=10):
        self.motor.set_rpm(rpm)
        self.motor.start()
        self.until(lambda: self.motor.state == "running")
        self.until(lambda: self.motor.commanded_rpm >= rpm - 0.01, seconds=6)
        self.until(lambda: abs(abs(self.motor.snapshot()["measured_rpm"]) - rpm) < 0.001)

    def report_speed(self, rpm):
        self.drive.drop.add(9)
        self.drive.rx.append((0x29, struct.pack("<ff", self.drive.position, rpm * 8 / 60)))
        self.step()

    def test_direction_metadata_is_passive_and_read_only(self):
        view = self.motor.snapshot()
        self.assertEqual(self.motor.direction, 1)
        self.assertTrue(self.motor.supports_reverse)
        self.assertTrue(self.motor.supports_stall_recovery)
        self.assertEqual(view["direction"], 1)
        self.assertTrue(view["supports_reverse"])
        self.assertTrue(view["supports_stall_recovery"])
        self.assertFalse(view["stall_recovery_enabled"])
        self.assertEqual(view["stall_timeout_s"], 0.25)
        json.dumps(view, allow_nan=False)
        for name in ("direction", "supports_reverse", "supports_stall_recovery"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                setattr(self.motor, name, -1)
        self.assertEqual(self.drive.attempts, [])

    def test_invalid_direction_preserves_target_and_sends_nothing(self):
        self.ready()
        self.motor.set_rpm(50)
        cutoff = len(self.drive.attempts)
        for value in (0, 2, -2, True, False, 1.0, -1.0, float("nan"), float("inf"), "-1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.motor.set_direction(value)
            self.assertEqual(self.motor.direction, 1)
            self.assertEqual(self.motor.target_rpm, 50)
        self.assertEqual(len(self.drive.attempts), cutoff)

    def test_reverse_is_explicit_and_keeps_speed_as_a_magnitude(self):
        self.ready()
        self.motor.set_rpm(110)
        cutoff = len(self.drive.attempts)
        self.motor.set_direction(-1)
        self.assertEqual(len(self.drive.attempts), cutoff)
        self.assertEqual(self.motor.state, "idle")
        self.assertEqual(self.motor.target_rpm, 110)
        self.assertTrue(self.motor.stop_confirmed)
        self.run_ready_motor(110)
        self.assertAlmostEqual(self.motor.snapshot()["measured_rpm"], -110, places=3)
        self.assertEqual(self.motor.commanded_rpm, 110)
        negative_commands = [(t, struct.unpack("<ff", data)) for t, cmd, data in self.drive.attempts
                             if cmd == 0x0D and struct.unpack("<f", data[:4])[0] < 0]
        arm = next(t for t, cmd, data in self.drive.delivered if cmd == 7 and data[0] == 8)
        self.assertGreater(negative_commands[0][0], arm + 0.01)
        self.assertAlmostEqual(negative_commands[-1][1][0], -110 * 8 / 60, places=4)
        self.assertTrue(all(torque == 0 for _, (_, torque) in negative_commands))
        self.motor.set_rpm(50)
        self.until(lambda: self.motor.commanded_rpm == 50, seconds=4)
        self.advance(1.1)
        self.assertIsNone(self.motor.fault)

    def test_direction_changes_require_idle_even_for_the_same_direction(self):
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(-1)
        self.motor.connect()
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(-1)
        self.until(lambda: self.motor.snapshot()["ready"])
        self.motor.set_direction(-1)
        self.motor.set_rpm(10)
        self.motor.start()
        for direction in (-1, 1):
            with self.assertRaises(RuntimeError):
                self.motor.set_direction(direction)
        self.until(lambda: self.motor.state == "running")
        self.until(lambda: self.motor.commanded_rpm >= 10)
        for direction in (-1, 1):
            with self.assertRaises(RuntimeError):
                self.motor.set_direction(direction)
        self.drive.coast_until = self.clock.now + 0.4
        self.motor.stop()
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(1)
        self.until(lambda: self.motor.stop_confirmed)
        self.motor.set_direction(1)
        self.assertEqual(self.motor.direction, 1)
        self.assertEqual(self.motor.state, "idle")

    def test_stale_feedback_and_overdue_idle_loop_reject_configuration(self):
        for gap in (0.11, 0.36):
            with self.subTest(gap=gap):
                self.setUp()
                self.ready()
                self.clock.now += gap
                cutoff = len(self.drive.attempts)
                with self.assertRaises(RuntimeError):
                    self.motor.set_direction(-1)
                with self.assertRaises(RuntimeError):
                    self.motor.configure_stall_recovery(True)
                self.assertEqual(self.motor.direction, 1)
                self.assertFalse(self.motor.snapshot()["stall_recovery_enabled"])
                self.assertEqual(len(self.drive.attempts), cutoff)

    def test_close_pending_and_closed_reject_direction_changes(self):
        self.ready()
        self.motor.close()
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(-1)
        self.until(lambda: self.motor.state == "closed")
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(-1)

    def test_reverse_stop_coasts_before_sending_canonical_zero(self):
        self.ready()
        self.motor.set_direction(-1)
        self.run_ready_motor()
        cutoff = len(self.drive.delivered)
        self.motor.set_rpm(0)
        self.until(lambda: self.motor.stop_confirmed)
        after = self.drive.delivered[cutoff:]
        self.assertEqual(after[0][1], 7)
        self.assertEqual(after[0][2][0], 1)
        self.assertTrue(any(cmd == 0x0D and data == bytes(8) for _, cmd, data in after))
        self.assertTrue(all(data == bytes(8) for _, cmd, data in after if cmd == 0x0D))
        self.assertEqual(self.motor.direction, -1)

    def test_wrong_way_guard_follows_selected_direction(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.setUp()
                self.ready()
                self.motor.set_direction(direction)
                self.run_ready_motor()
                self.report_speed(-direction * 2.01)
                self.assertIn("speed/direction", self.motor.fault)

    def test_absolute_overspeed_guard_applies_in_both_directions(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.setUp()
                self.ready()
                self.motor.set_direction(direction)
                self.run_ready_motor()
                self.report_speed(direction * 136)
                self.assertIn("speed/direction", self.motor.fault)

    def test_reverse_stall_latches_and_cannot_change_direction_or_clear_itself(self):
        self.ready()
        self.motor.set_direction(-1)
        self.run_ready_motor(5)
        self.advance(1.1)
        self.drive.stall, self.drive.rps = True, 0.0
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("stalled", self.motor.fault)
        self.until(lambda: self.motor.stop_confirmed)
        cutoff = len(self.drive.attempts)
        with self.assertRaises(RuntimeError):
            self.motor.set_direction(1)
        with self.assertRaises(RuntimeError):
            self.motor.configure_stall_recovery(True)
        self.advance(1)
        self.assertEqual(self.motor.state, "fault")
        self.assertEqual(self.motor.direction, -1)
        self.assertFalse(any(cmd == 0x18 or (cmd == 7 and data[0] == 8)
                             for _, cmd, data in self.drive.attempts[cutoff:]))

    def test_reverse_retains_sustained_current_guard(self):
        self.ready()
        self.motor.set_direction(-1)
        self.run_ready_motor()
        self.drive.iq = (0.5, 0.9)
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("current", self.motor.fault)

    def test_stall_recovery_configuration_validates_and_is_idle_only(self):
        self.ready()
        cutoff = len(self.drive.attempts)
        for enabled in (0, 1, "true", None):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                self.motor.configure_stall_recovery(enabled)
        for detection in (0.09, 3.01, True, float("nan"), float("inf"), "0.4", None):
            with self.subTest(detection=detection), self.assertRaises(ValueError):
                self.motor.configure_stall_recovery(True, detection)
        self.motor.configure_stall_recovery(True, 3)
        self.assertEqual(self.motor.snapshot()["stall_timeout_s"], 3.3)
        self.motor.configure_stall_recovery(True, 0.1)
        self.assertEqual(self.motor.snapshot()["stall_detection_s"], 0.1)
        self.assertEqual(self.motor.snapshot()["stall_timeout_s"], 0.4)
        self.assertEqual(len(self.drive.attempts), cutoff)
        self.motor.set_rpm(10)
        self.motor.start()
        with self.assertRaises(RuntimeError):
            self.motor.configure_stall_recovery(False)
        self.until(lambda: self.motor.state == "running")
        with self.assertRaises(RuntimeError):
            self.motor.configure_stall_recovery(False)
        self.motor.stop()
        with self.assertRaises(RuntimeError):
            self.motor.configure_stall_recovery(False)

    def test_enabled_recovery_still_faults_if_external_supervisor_does_nothing(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.setUp()
                self.ready()
                self.motor.set_direction(direction)
                self.motor.configure_stall_recovery(True, 0.4)
                self.run_ready_motor()
                self.advance(1.1)
                self.drive.stall, self.drive.rps = True, 0.0
                started = self.clock.now
                self.advance(0.45)
                self.assertIsNone(self.motor.fault)
                self.until(lambda: self.motor.fault is not None, seconds=0.5)
                self.assertIn("stalled", self.motor.fault)
                self.assertGreaterEqual(self.clock.now - started, 0.7)
                self.assertLess(self.clock.now - started, 0.76)
                self.until(lambda: self.motor.stop_confirmed)
                cutoff = len(self.drive.attempts)
                self.advance(1)
                self.assertEqual(self.motor.state, "fault")
                self.assertFalse(any(cmd == 7 and data[0] == 8
                                     for _, cmd, data in self.drive.attempts[cutoff:]))

    def test_disabling_recovery_restores_default_hard_stall_timeout(self):
        self.ready()
        self.motor.configure_stall_recovery(True, 3)
        self.motor.configure_stall_recovery(False)
        self.assertFalse(self.motor.snapshot()["stall_recovery_enabled"])
        self.assertEqual(self.motor.snapshot()["stall_timeout_s"], 0.25)
        self.run_ready_motor()
        self.advance(1.1)
        self.drive.stall, self.drive.rps = True, 0.0
        started = self.clock.now
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("stalled", self.motor.fault)
        self.assertGreaterEqual(self.clock.now - started, 0.25)
        self.assertLess(self.clock.now - started, 0.31)

    def test_external_stop_within_recovery_window_leaves_a_healthy_idle(self):
        self.ready()
        self.motor.configure_stall_recovery(True, 0.4)
        self.run_ready_motor()
        self.advance(1.1)
        self.drive.stall, self.drive.rps = True, 0.0
        self.advance(0.45)
        self.assertIsNone(self.motor.fault)
        self.motor.stop()
        self.until(lambda: self.motor.stop_confirmed)
        self.motor.set_direction(-1)
        cutoff = len(self.drive.attempts)
        self.advance(1)
        self.assertEqual(self.motor.state, "idle")
        self.assertIsNone(self.motor.fault)
        self.assertEqual(self.motor.direction, -1)
        self.assertFalse(any(cmd == 7 and data[0] == 8
                             for _, cmd, data in self.drive.attempts[cutoff:]))


if __name__ == "__main__":
    unittest.main()
