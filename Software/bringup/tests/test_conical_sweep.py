"""Fixed sweep simulation with telemetry, coasting and tracking failures."""

from pathlib import Path
import struct
import unittest

from test_motor_test import APP, Clock, MotorBus, message


SCOPE = {"__name__": "conical_profile", "MotorTrial": APP.MotorTrial,
         "FIELDS": APP.FIELDS, "GEAR_RATIO": APP.GEAR_RATIO, "WD_ERROR": APP.WD_ERROR}
exec((Path(__file__).resolve().parents[1] / "conical_sweep.py").read_text(), SCOPE)
ConicalSweep = SCOPE["ConicalSweep"]


class SweepBus(MotorBus):
    def __init__(self, clock):
        super().__init__(clock)
        self.values.update({289: 2.0, 246: 1.0, 186: 0, 188: 0})
        self.formats.update({186: "I", 188: "I"})
        self.next_bus = self.next_iq = 0.0
        self.gain = 1.0
        self.drop_telemetry = False
        self.bus_voltage = 24.2
        self.iq = 0.3
        self.coast_started = self.stationary_reported = None
        self.cleanup_events = []
        self.coast_deceleration = 5.0
        self.fail_idle_once = False
        self.idle_observed = []
        self.single_high_then_drop_iq = False
        self.high_iq_sent = False

    def advance(self):
        elapsed = self.clock.now - self.last_update
        super().advance()
        if self.state == 1 and self.coast_started is not None:
            self.velocity = max(0.0, self.velocity - self.coast_deceleration * elapsed)

    def read_message(self):
        frame = super().read_message()
        if frame is not None:
            if frame.id == 0x21 and frame.data[4] == 1:
                self.idle_observed.append(self.clock.now)
            if frame.id == 0x29 and self.coast_started is not None and abs(self.velocity) < 0.15:
                self.stationary_reported = self.clock.now
            return frame
        if self.drop_telemetry and self.nonzero_events:
            return None
        now = self.clock.now
        if self.values[188] and now >= self.next_bus:
            self.next_bus = now + self.values[188] / 1000
            return message(0x37, struct.pack("<ff", self.bus_voltage, -0.1))
        if self.values[186] and now >= self.next_iq:
            self.next_iq = now + self.values[186] / 1000
            if self.single_high_then_drop_iq and self.nonzero_events:
                if self.high_iq_sent:
                    return None
                self.high_iq_sent = True
                return message(0x34, struct.pack("<ff", 1.0, 1.3))
            return message(0x34, struct.pack("<ff", self.iq, self.iq))
        return None

    def send(self, frame):
        command = frame.id & 0x1F
        data = frame.data
        if command == 0x07 and data[:4] == b"\x01\0\0\0" and self.fail_idle_once:
            self.fail_idle_once = False
            self.attempts.append((self.clock.now, frame))
            raise OSError("injected lost IDLE command")
        if command == 0x0D:
            self.advance()
            velocity, torque = struct.unpack("<ff", data)
            assert 0 <= velocity <= 110 * 8 / 60 + 0.00001 and torque == 0
            self.attempts.append((self.clock.now, frame))
            self.values[267], self.values[268] = velocity, torque
            if velocity:
                self.nonzero_events.append(self.clock.now)
            if self.state == 8:
                self.velocity = velocity * self.gain
            self.last_feed = self.clock.now
            return
        if command == 0x0F:
            self.advance()
            velocity_limit, current_limit = struct.unpack("<ff", data)
            assert 0 < velocity_limit <= 130 * 8 / 60 + 0.00001
            assert 0 < current_limit <= 1
            if self.limits_written and velocity_limit == 2:
                assert self.state == 1 and self.stationary_reported is not None
                self.cleanup_events.append((self.clock.now, "velocity_limit"))
            self.values[289], self.values[246] = velocity_limit, current_limit
            self.limits_written = True
            self.attempts.append((self.clock.now, frame))
            self.last_feed = self.clock.now
            return
        endpoint = struct.unpack("<H", data[1:3])[0] if command == 0x05 else None
        if endpoint in (186, 188):
            self.advance()
            assert data[0] == 1 and data[3] == 0
            value = struct.unpack("<I", data[4:])[0]
            assert value in (0, 100)
            if value == 0:
                assert self.state == 1 and self.stationary_reported is not None
                self.cleanup_events.append((self.clock.now, endpoint))
            self.values[endpoint] = value
            self.attempts.append((self.clock.now, frame))
            self.last_feed = self.clock.now
            return
        previous_velocity = self.velocity
        super().send(frame)
        if command == 0x07 and data[:4] == b"\x01\0\0\0":
            self.coast_started = self.clock.now
            self.velocity = previous_velocity


class ConicalSweepTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bus = SweepBus(self.clock)
        self.output = []
        self.trial = ConicalSweep(self.bus, message, self.clock, self.output.append)

    def targets(self):
        return [(when, struct.unpack("<f", frame.data[:4])[0])
                for when, frame in self.bus.attempts if frame.id == 0x2D]

    def test_fixed_stages_reach_110_and_restore_low_limit_only_after_stationary(self):
        result = self.trial.run(move=True)
        self.assertEqual([stage["target"] for stage in result["stages"]], [20, 50, 80, 110])
        for stage in result["stages"]:
            self.assertTrue(stage["pass"])
            self.assertGreaterEqual(stage["samples"], 50)
            self.assertAlmostEqual(stage["meanrpm"], stage["target"], places=3)
        self.assertAlmostEqual(max(target for _, target in self.targets()), 14.6666667, places=5)
        self.assertTrue(any(line.startswith("SWEEP_SAMPLE ") for line in self.output))
        self.assertLess(self.clock.now - self.trial.motion_start, 35)
        self.assertEqual(self.bus.values[289], 2)
        self.assertEqual((self.bus.values[186], self.bus.values[188]), (0, 0))
        self.assertTrue(self.bus.cleanup_events)
        self.assertTrue(all(when >= self.bus.stationary_reported - 0.2
                            for when, _ in self.bus.cleanup_events))
        # The commanded deceleration progresses in bounded increments to zero.
        peak = max(range(len(self.targets())), key=lambda index: self.targets()[index][1])
        descending = self.targets()[peak:]
        self.assertTrue(any(0 < target < 1 for _, target in descending))

    def test_tracking_failure_refuses_later_stages_and_coasts_before_zero(self):
        self.bus.gain = 0.5
        with self.assertRaisesRegex(RuntimeError, "tracking failed at 20"):
            self.trial.run(move=True)
        self.assertLessEqual(max(target for _, target in self.targets()), 20 * 8 / 60 + 0.00001)
        stop_index = next(index for index, (_, frame) in enumerate(self.bus.attempts)
                          if frame.id == 0x27 and frame.data[:4] == b"\x01\0\0\0")
        self.assertEqual(self.bus.attempts[stop_index + 1][1].id, 0x2D)
        self.assertEqual(self.bus.attempts[stop_index + 1][1].data, bytes(8))
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.values[289], 2)

    def test_missing_current_voltage_stream_aborts_and_restores_after_stop(self):
        self.bus.drop_telemetry = True
        with self.assertRaisesRegex(RuntimeError, "telemetry timeout"):
            self.trial.run(move=True)
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.values[289], 2)
        self.assertEqual((self.bus.values[186], self.bus.values[188]), (0, 0))

    def test_sustained_excess_iq_aborts_without_increasing_current(self):
        self.bus.iq = 1.3
        with self.assertRaisesRegex(RuntimeError, "Measured Iq"):
            self.trial.run(move=True)
        self.assertLess(self.clock.now - self.trial.motion_start, 2)
        self.assertLessEqual(self.bus.values[246], 1)

    def test_rate_allowlist_refuses_arbitrary_configuration(self):
        with self.assertRaises(ValueError):
            self.trial.write("iq_rate_ms", 20)
        self.assertEqual(self.bus.attempts, [])

    def prepare_coasting_stop(self):
        self.trial.original = {name: self.bus.values[endpoint]
                               for name, (endpoint, _) in self.trial.fields.items()}
        self.bus.values.update({150: 0.5, 151: True, 186: 100, 188: 100, 267: 4.0})
        self.bus.state = self.trial.state = 8
        self.bus.velocity = self.trial.velocity = 4.0
        self.trial.last_hb = self.trial.last_encoder = self.clock.now

    def test_slow_coast_keeps_watchdog_fed_after_fresh_idle_before_zero(self):
        self.prepare_coasting_stop()
        self.bus.coast_deceleration = 1.0
        self.trial.stop()
        self.assertGreater(self.clock.now, 3)
        self.assertEqual(self.bus.error, 0)
        self.assertEqual(self.bus.watchdog_expiries, [])
        self.assertFalse(self.bus.values[151])
        self.assertEqual(self.bus.values[267], 0)
        self.assertGreaterEqual(self.targets()[0][0], self.bus.idle_observed[0])
        self.assertEqual(self.bus.values[289], 2)

    def test_lost_idle_command_waits_for_fresh_watchdog_disarm_before_any_zero(self):
        self.prepare_coasting_stop()
        self.bus.fail_idle_once = True
        with self.assertRaisesRegex(RuntimeError, "retains watchdog fault"):
            self.trial.stop()
        self.assertEqual(len(self.bus.watchdog_expiries), 1)
        self.assertTrue(self.bus.idle_observed)
        self.assertTrue(self.targets())
        self.assertGreaterEqual(self.targets()[0][0], self.bus.idle_observed[0])
        self.assertTrue(self.bus.values[151])
        self.assertEqual(self.bus.cleanup_events, [])

    def test_one_high_iq_sample_then_missing_stream_is_telemetry_timeout(self):
        self.bus.single_high_then_drop_iq = True
        with self.assertRaisesRegex(RuntimeError, "telemetry timeout"):
            self.trial.run(move=True)
        self.assertTrue(self.bus.high_iq_sent)
        self.assertFalse(self.trial.overcurrent)


if __name__ == "__main__":
    unittest.main()
