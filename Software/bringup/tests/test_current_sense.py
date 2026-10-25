"""Current-only profile checks with signed noisy telemetry and a timed stall."""

from pathlib import Path
import struct
import unittest

from test_conical_sweep import ConicalSweep, SweepBus
from test_motor_test import APP, Clock, message


SCOPE = {"__name__": "current_profile", "ConicalSweep": ConicalSweep,
         "GEAR_RATIO": APP.GEAR_RATIO}
exec((Path(__file__).resolve().parents[1] / "current_sense.py").read_text(), SCOPE)
CurrentSense = SCOPE["CurrentSense"]


class CurrentBus(SweepBus):
    def __init__(self, clock):
        super().__init__(clock)
        self.profile = None
        self.iq_samples = 0
        self.sign = 1
        self.stall_after = None
        self.baseline_iq = 0.1

    def advance(self):
        if (self.stall_after is not None and self.profile.motion_start is not None
                and self.clock.now - self.profile.motion_start >= self.stall_after):
            self.gain = 0.1
        super().advance()

    def read_message(self):
        frame = super().read_message()
        if frame is not None and frame.id == 0x34:
            self.iq_samples += 1
            base = 0.3 if self.profile.phase == "load" else self.baseline_iq
            noise = 0.02 if self.iq_samples % 2 else -0.02
            measured = self.sign * (base + noise)
            return message(0x34, struct.pack("<ff", measured + 0.025, measured))
        return frame

    def send(self, frame):
        command = frame.id & 0x1F
        if command == 0x0D:
            target, torque = struct.unpack("<ff", frame.data)
            assert 0 <= target <= 10 * 8 / 60 + 0.00001 and torque == 0
        elif command == 0x0F:
            speed, current = struct.unpack("<ff", frame.data)
            assert speed <= 2 and current <= 0.5
        elif command == 0x05 and frame.data[1:3] == struct.pack("<H", 186):
            rate = struct.unpack("<I", frame.data[4:])[0]
            assert rate in (0, 20)
            if rate == 0:
                assert self.state == 1 and self.stationary_reported is not None
            self.values[186] = rate
            self.attempts.append((self.clock.now, frame))
            self.last_feed = self.clock.now
            return
        super().send(frame)


class CurrentSenseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bus = CurrentBus(self.clock)
        self.output = []
        self.trial = CurrentSense(self.bus, message, self.clock, self.output.append)
        self.bus.profile = self.trial

    def parsed_samples(self):
        return [dict(field.split("=", 1) for field in line.split()[1:])
                for line in self.output if line.startswith("CURRENT_SAMPLE ")]

    def test_noisy_baseline_and_load_report_signed_pair_and_averaged_trend(self):
        result = self.trial.run(move=True)
        self.assertTrue(result["completed"])
        self.assertAlmostEqual(result["baseline_iq_mean"], 0.1, delta=0.01)
        self.assertAlmostEqual(result["load_iq_mean"], 0.3, delta=0.01)
        self.assertAlmostEqual(result["load_minus_baseline_iq"], 0.2, delta=0.01)
        self.assertGreaterEqual(result["baseline_samples"], 100)
        self.assertGreater(result["load_samples"], 2800)
        samples = self.parsed_samples()
        self.assertEqual(set(sample["phase"] for sample in samples), {"ramp", "baseline", "load"})
        baseline = [sample for sample in samples if sample["phase"] == "baseline"]
        raw_error = sum(abs(float(sample["iq"]) - 0.1) for sample in baseline)
        mean_error = sum(abs(float(sample["iq_mean"]) - 0.1) for sample in baseline)
        self.assertLess(mean_error, raw_error)
        self.assertAlmostEqual(float(samples[-1]["iq_set"]) - float(samples[-1]["iq"]), 0.025, places=4)
        self.assertLess(self.clock.now - self.trial.motion_start, 75)
        self.assertLess(result["output_revolutions"], 15)
        self.assertLessEqual(self.bus.values[246], 0.5)
        self.assertEqual(self.bus.values[186], 0)
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.error, 0)
        self.assertTrue(self.trial.stall_armed)
        self.assertGreaterEqual(len(result["baseline"]["blocks_500ms"]), 4)
        self.assertGreaterEqual(len(result["load"]["blocks_500ms"]), 10)
        self.assertAlmostEqual(result["baseline"]["mean_iq_set"] - result["baseline"]["mean_iq"],
                               0.025, places=5)
        self.assertAlmostEqual(result["load_minus_baseline_iq_set"], 0.2, delta=0.01)
        self.assertTrue(any(line == "CURRENT_PHASE baseline: keep brake released" for line in self.output))
        self.assertTrue(any(line == "CURRENT_PHASE load: apply light fixture load now" for line in self.output))

    def test_negative_iq_is_preserved_in_baseline_load_and_delta(self):
        self.bus.sign = -1
        result = self.trial.run(move=True)
        self.assertAlmostEqual(result["baseline_iq_mean"], -0.1, delta=0.01)
        self.assertAlmostEqual(result["load_iq_mean"], -0.3, delta=0.01)
        self.assertLess(result["load_minus_baseline_iq"], -0.18)

    def test_sustained_load_stall_coasts_without_retry_or_current_increase(self):
        self.bus.stall_after = 4.5
        with self.assertRaisesRegex(RuntimeError, "Sustained low speed"):
            self.trial.run(move=True)
        self.assertLess(self.clock.now - self.trial.motion_start, 5.5)
        self.assertEqual(len(self.bus.activation_events), 2)
        self.assertLessEqual(self.bus.values[246], 0.5)
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.values[267], 0)

    def test_missing_live_current_telemetry_stops_profile(self):
        self.bus.drop_telemetry = True
        with self.assertRaisesRegex(RuntimeError, "telemetry timeout"):
            self.trial.run(move=True)
        self.assertLess(self.clock.now - self.trial.motion_start, 1)
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.values[186], 0)

    def test_import_has_no_motion_and_iq_rate_is_fixed(self):
        self.assertEqual(self.bus.attempts, [])
        for rate in (10, 50, 100):
            with self.subTest(rate=rate):
                with self.assertRaises(ValueError):
                    self.trial.write("iq_rate_ms", rate)
        self.assertEqual(self.bus.attempts, [])

    def test_baseline_without_current_headroom_refuses_load_window(self):
        self.bus.baseline_iq = 0.42
        with self.assertRaisesRegex(RuntimeError, "Baseline lacks"):
            self.trial.run(move=True)
        self.assertFalse(any("CURRENT_PHASE load:" in line for line in self.output))
        self.assertEqual(self.bus.state, 1)

    def test_baseline_below_eight_rpm_never_arms_stall_or_opens_load_window(self):
        self.bus.gain = 0.7
        with self.assertRaisesRegex(RuntimeError, "Baseline lacks"):
            self.trial.run(move=True)
        self.assertFalse(self.trial.stall_armed)
        self.assertFalse(any("CURRENT_PHASE load:" in line for line in self.output))
        self.assertEqual(self.bus.state, 1)


if __name__ == "__main__":
    unittest.main()
