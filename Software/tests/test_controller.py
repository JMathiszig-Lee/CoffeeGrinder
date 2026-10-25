"""UI behavior against a timed drive peer, not a mock of controller internals."""

from pathlib import Path
import json
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_api import GrinderController
from grinder_api import protocol


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Drive:
    """Independent CAN peer with TX delay, telemetry timers and drive watchdog."""

    def __init__(self, clock):
        self.clock = clock
        self.values = {2: 24.0, 9: 0, 10: 5, 11: 14, 12: 1, 147: False,
                       149: False, 150: 0.0, 151: False, 179: 100, 180: 10,
                       186: 0, 188: 0, 192: True, 246: 30.0, 267: 0.0,
                       268: 0.0, 279: False, 282: False, 283: 3, 284: 1,
                       289: 30.0, 330: True}
        self.formats = {endpoint: "B" if codec == "?" else codec
                        for endpoint, codec in protocol.FIELDS.values()}
        self.rx, self.attempts, self.delivered = [], [], []
        self._tx = None
        self.closed = False
        self.state, self.error, self.flags, self.life = 1, 0, 0x80, 0
        self.rps, self.position = 0.0, 0.0
        self.last_step = self.last_feed = 0.0
        self.next_due = {1: 0.0, 9: 0.0, 0x14: 0.0, 0x17: 0.0}
        self.drop = set()
        self.readback = {}
        self.drop_idle = False
        self.block_tx = False
        self.coast_until = None
        self.stall = False
        self.iq = (-0.2, -0.18)
        self.rx_overflows = 0

    @property
    def pending(self):
        return self._tx is not None

    @property
    def diagnostics(self):
        return {"rx_overflows": self.rx_overflows}

    def send(self, can_id, data):
        if self.pending:
            return False
        self.attempts.append((self.clock.now, can_id & 31, bytes(data)))
        self._tx = (self.clock.now + 0.001, can_id & 31, bytes(data))
        return True

    def abort(self):
        self._tx = None

    def close(self):
        self.abort()
        self.closed = True

    def _deliver(self, cmd, data):
        now = self.clock.now
        self.last_feed = now
        self.delivered.append((now, cmd, data))
        if cmd in (4, 5):
            opcode, endpoint, reserved = struct.unpack("<BHB", data[:4])
            assert reserved == 0 and opcode == (0 if cmd == 4 else 1)
            codec = self.formats[endpoint]
            if cmd == 4:
                if endpoint in self.drop:
                    return
                value = self.readback.get(endpoint, self.values[endpoint])
                raw = struct.pack("<" + codec, value)
                self.rx.append((0x24, data[:4] + raw + bytes(4 - len(raw))))
            else:
                self.values[endpoint] = struct.unpack("<" + codec, data[4:4 + struct.calcsize(codec)])[0]
        elif cmd == 7:
            state = struct.unpack("<I", data[:4])[0]
            assert state in (1, 8)
            if state == 1 and self.drop_idle:
                return
            self.state = state
            if state == 1 and self.coast_until is None:
                self.rps = 0.0
        elif cmd == 0x0B:
            self.values[283], self.values[284] = struct.unpack("<II", data)
        elif cmd == 0x0D:
            self.values[267], self.values[268] = struct.unpack("<ff", data)
            if self.state == 8:
                self.rps = 0.0 if self.stall else self.values[267]
        elif cmd == 0x0F:
            self.values[289], self.values[246] = struct.unpack("<ff", data)
        else:
            raise AssertionError("Unexpected drive command %x" % cmd)

    def update(self):
        now = self.clock.now
        self.position += self.rps * (now - self.last_step)
        self.last_step = now
        if self.pending and now >= self._tx[0] and not self.block_tx:
            _, cmd, data = self._tx
            self._tx = None
            self._deliver(cmd, data)
        if self.values[151] and now - self.last_feed > self.values[150]:
            self.error, self.state, self.rps = 0x800, 1, 0.0
        if self.coast_until is not None and now >= self.coast_until:
            self.rps = 0.0
        if self.closed:
            return
        for cmd, interval_ep in ((1, 179), (9, 180), (0x14, 186), (0x17, 188)):
            interval = self.values[interval_ep] / 1000
            if not interval or cmd in self.drop or now < self.next_due[cmd]:
                continue
            self.next_due[cmd] = now + interval
            if cmd == 1:
                self.life = (self.life + 1) & 255
                data = struct.pack("<IBBBB", self.error, self.state, self.flags, 0, self.life)
            elif cmd == 9:
                data = struct.pack("<ff", self.position, self.rps)
            elif cmd == 0x14:
                data = struct.pack("<ff", *self.iq)
            else:
                data = struct.pack("<ff", self.values[2], 0.1)
            self.rx.append((0x20 | cmd, data))

    def receive(self):
        return self.rx.pop(0) if self.rx else None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.drive = Drive(self.clock)
        self.motor = GrinderController(self.drive, clock=self.clock)

    def step(self, seconds=0.005):
        self.clock.now += seconds
        self.motor.update()

    def until(self, predicate, seconds=3):
        end = self.clock.now + seconds
        while self.clock.now < end and not predicate():
            self.step()
        self.assertTrue(predicate(), self.motor.snapshot())

    def ready(self):
        self.motor.connect()
        self.until(lambda: self.motor.snapshot()["ready"])

    def run_motor(self, rpm=10):
        self.ready()
        self.motor.set_rpm(rpm)
        self.motor.start()
        self.until(lambda: self.motor.state == "running")
        self.until(lambda: self.motor.commanded_rpm >= rpm - 0.01, seconds=6)

    def test_constructor_is_passive_and_configuration_never_starts(self):
        self.assertEqual(self.drive.attempts, [])
        self.ready()
        self.assertEqual(self.drive.state, 1)
        self.assertFalse(any(c == 7 and struct.unpack("<I", d[:4])[0] == 8
                             for _, c, d in self.drive.attempts))
        self.assertAlmostEqual(self.drive.values[246], 0.5)
        self.assertAlmostEqual(self.drive.values[289], 130 * 8 / 60, places=4)
        self.assertTrue(self.drive.values[151])
        self.assertEqual((self.drive.values[179], self.drive.values[180], self.drive.values[186], self.drive.values[188]),
                         (100, 20, 50, 100))

    def test_firmware_mismatch_has_no_endpoint_writes_or_closed_loop(self):
        self.drive.values[11] = 13
        self.motor.connect()
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("firmware", self.motor.fault)
        self.assertFalse(any(c in (5, 0x0B, 0x0D, 0x0F) for _, c, _ in self.drive.attempts))

    def test_limit_readback_mismatch_prevents_start(self):
        self.drive.readback[289] = 30.0
        self.motor.connect()
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("readback", self.motor.fault)
        with self.assertRaises(RuntimeError):
            self.motor.start()
        self.assertFalse(any(c == 7 and d[0] == 8 for _, c, d in self.drive.attempts))

    def test_existing_lower_current_limit_is_never_raised(self):
        self.drive.values[246] = 0.25
        self.ready()
        self.assertEqual(self.motor.current_limit_a, 0.25)
        self.assertEqual(self.drive.values[246], 0.25)

    def test_invalid_rpm_never_changes_target_or_transmits(self):
        self.motor.set_rpm(50)
        for value in (-1, 0.1, 4, 111, 10**500, float("nan"), float("inf"), True, "10", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.motor.set_rpm(value)
            self.assertEqual(self.motor.target_rpm, 50)
        self.assertEqual(self.drive.attempts, [])

    def test_start_is_explicit_and_cannot_be_queued_during_connect(self):
        self.motor.set_rpm(110)
        self.motor.connect()
        with self.assertRaises(RuntimeError):
            self.motor.start()
        self.until(lambda: self.motor.state == "idle")
        self.assertEqual(self.drive.state, 1)

    def test_start_waits_for_fresh_closed_loop_before_ramp_and_reaches_110(self):
        self.run_motor(110)
        positives = [(t, d) for t, c, d in self.drive.attempts if c == 0x0D and struct.unpack("<f", d[:4])[0] > 0]
        arm = next(t for t, c, d in self.drive.delivered if c == 7 and d[0] == 8)
        self.assertGreater(positives[0][0], arm + 0.01)
        self.until(lambda: abs(self.motor.snapshot()["measured_rpm"] - 110) < 0.001)
        self.assertAlmostEqual(self.motor.snapshot()["measured_rpm"], 110, places=3)
        self.motor.set_rpm(50)
        self.until(lambda: self.motor.commanded_rpm == 50, seconds=4)

    def test_stop_during_start_cancels_arm_and_positive_targets(self):
        self.ready()
        self.motor.set_rpm(50)
        self.motor.start()
        self.step()
        self.motor.stop()
        self.until(lambda: self.motor.snapshot()["stop_confirmed"])
        self.assertFalse(any(c == 7 and d[0] == 8 for _, c, d in self.drive.delivered))
        self.assertFalse(any(c == 0x0D and struct.unpack("<f", d[:4])[0] > 0 for _, c, d in self.drive.attempts))

    def test_stop_during_config_cancels_configuration_and_requires_reconnect(self):
        self.motor.connect()
        self.step()
        self.motor.stop()
        self.until(lambda: self.motor.state == "disconnected")
        self.assertFalse(any(c == 5 for _, c, _ in self.drive.attempts))
        with self.assertRaises(RuntimeError):
            self.motor.start()

    def test_cancel_late_connection_does_not_expire_an_owned_watchdog(self):
        self.motor.connect()
        self.until(lambda: self.drive.values[151])
        self.assertEqual(self.motor.state, "connecting")
        self.motor.stop()
        self.until(lambda: self.motor.state == "disconnected")
        for _ in range(200):
            self.step()
        self.assertEqual(self.drive.error, 0)
        self.motor.close()
        self.until(lambda: self.motor.state == "closed")
        self.assertFalse(self.drive.values[151])

    def test_zero_rpm_coasts_instead_of_active_braking(self):
        self.run_motor()
        cutoff = len(self.drive.delivered)
        self.motor.set_rpm(0)
        self.until(lambda: self.motor.state == "idle")
        after = self.drive.delivered[cutoff:]
        self.assertEqual(after[0][1], 7)
        self.assertEqual(after[0][2][0], 1)
        self.assertTrue(any(c == 0x0D and d == bytes(8) for _, c, d in after))

    def test_lost_idle_never_sends_zero_while_still_closed_loop(self):
        self.run_motor()
        self.drive.drop_idle = True
        cutoff = len(self.drive.attempts)
        self.motor.stop()
        self.until(lambda: self.motor.fault is not None, seconds=2)
        self.assertEqual(self.drive.error, 0x800)
        expiry_time = self.drive.last_feed + 0.5
        early_velocities = [(t, d) for t, c, d in self.drive.attempts[cutoff:]
                            if c == 0x0D and t < expiry_time]
        self.assertEqual(early_velocities, [])

    def test_slow_loop_faults_before_buffered_frames_or_keepalive(self):
        self.run_motor()
        cutoff = len(self.drive.attempts)
        self.step(0.2)
        self.assertIn("deadline", self.motor.fault)
        self.assertFalse(any(c == 0x0D for _, c, _ in self.drive.attempts[cutoff:]))

    def test_stale_iq_faults_and_snapshot_uses_none(self):
        self.run_motor()
        self.drive.drop.add(0x14)
        self.until(lambda: self.motor.fault is not None)
        view = self.motor.snapshot()
        self.assertIn("stale", view["fault"])
        self.assertIsNone(view["load_percent"])
        self.assertIsNone(view["iq_measured_a"])
        self.assertFalse(view["telemetry_valid"])

    def test_receive_overflow_during_motion_latches_fault(self):
        self.run_motor()
        self.drive.rx_overflows += 1
        self.step()
        self.assertIn("overflow", self.motor.fault)

    def test_stall_has_no_retry_and_local_clear_does_not_restart(self):
        self.run_motor()
        self.drive.stall, self.drive.rps = True, 0.0
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("stalled", self.motor.fault)
        self.until(lambda: self.motor.snapshot()["stop_confirmed"])
        cutoff = len(self.drive.attempts)
        self.motor.clear_fault()
        self.assertEqual(self.motor.target_rpm, 0)
        self.assertFalse(any(c == 7 and d[0] == 8 for _, c, d in self.drive.attempts[cutoff:]))

    def test_drive_error_cannot_be_cleared_by_ui(self):
        self.run_motor()
        self.drive.error, self.drive.flags = 0x40, 1
        self.until(lambda: self.motor.fault is not None)
        self.until(lambda: self.motor.snapshot()["stop_confirmed"])
        with self.assertRaises(RuntimeError):
            self.motor.clear_fault()
        self.assertFalse(any(c == 0x18 for _, c, _ in self.drive.attempts))

    def test_signed_filtered_current_is_a_demand_proxy_not_measured_current(self):
        self.run_motor()
        self.drive.iq = (-0.25, -0.4)
        for _ in range(200):
            self.step()
        snapshot = self.motor.snapshot()
        self.assertAlmostEqual(snapshot["load_percent"], 50, delta=0.5)
        self.assertLess(snapshot["iq_setpoint_filtered_a"], 0)
        self.assertAlmostEqual(snapshot["iq_measured_filtered_a"], -0.4, delta=0.01)
        self.assertEqual(snapshot["load_source"], "current_demand")
        json.dumps(snapshot, allow_nan=False)
        snapshot["diagnostics"]["rx_overflows"] = 123
        self.assertEqual(self.drive.rx_overflows, 0)

    def test_idle_current_is_not_presented_as_an_active_load(self):
        self.ready()
        self.assertIsNone(self.motor.snapshot()["load_percent"])
        self.assertFalse(self.motor.snapshot()["load_valid"])
        self.assertIsNotNone(self.motor.snapshot()["iq_measured_a"])

    def test_public_state_and_configuration_are_read_only(self):
        for name in ("state", "fault", "max_rpm", "gear_ratio", "current_limit_a", "requested_current_limit", "target_rpm", "commanded_rpm", "stop_confirmed"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                setattr(self.motor, name, 999)

    def test_close_waits_for_coast_and_transport_completion(self):
        self.run_motor()
        self.drive.coast_until = self.clock.now + 0.7
        self.motor.close()
        self.step()
        self.assertFalse(self.drive.closed)
        self.until(lambda: self.motor.state == "closed", seconds=2)
        self.assertTrue(self.motor.snapshot()["stop_confirmed"])
        self.assertEqual(self.drive.state, 1)
        self.assertEqual(self.drive.rps, 0)
        self.assertFalse(self.drive.values[151])
        disabled = next(t for t, c, d in self.drive.delivered if c == 5 and d[1:3] == struct.pack("<H", 151) and d[4] == 0)
        self.assertGreaterEqual(disabled, self.drive.coast_until)

    def test_clean_close_does_not_leave_a_watchdog_fault_and_can_reconnect(self):
        self.ready()
        self.motor.close()
        self.until(lambda: self.motor.state == "closed")
        self.clock.now += 1
        self.drive.update()
        self.assertEqual(self.drive.error, 0)
        self.drive.closed = False
        self.motor = GrinderController(self.drive, clock=self.clock)
        self.ready()

    def test_stop_during_close_retries_aborted_watchdog_disable(self):
        self.ready()
        self.motor.close()
        self.until(lambda: any(c == 5 and d[1:3] == struct.pack("<H", 151) and d[4] == 0
                               for _, c, d in self.drive.attempts))
        self.motor.stop()  # Abort the queued disable before the peer gets it.
        self.until(lambda: self.motor.state == "closed")
        self.assertFalse(self.drive.values[151])

    def test_stop_attempt_recovers_even_without_watchdog_after_bus_returns(self):
        self.run_motor()
        self.drive.values[151] = False
        self.drive.drop_idle = True
        self.motor.stop()
        for _ in range(180):
            self.step()
        self.assertEqual(self.drive.state, 8)
        self.drive.drop_idle = False
        self.until(lambda: self.motor.snapshot()["stop_confirmed"], seconds=2)
        self.assertEqual(self.drive.state, 1)

    def test_one_high_current_sample_is_not_treated_as_sustained(self):
        self.run_motor()
        self.drive.rx.append((0x34, struct.pack("<ff", -0.2, -0.9)))
        self.drive.drop.add(0x14)
        self.step()
        for _ in range(37):
            self.step()
        self.assertIsNone(self.motor.fault)
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("stale", self.motor.fault)

    def test_repeated_high_current_samples_trip_the_current_guard(self):
        self.run_motor()
        self.drive.iq = (-0.5, -0.9)
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("current", self.motor.fault)

    def test_config_timeout_latches_fault_without_arming(self):
        self.drive.drop.add(11)
        self.motor.connect()
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("timeout", self.motor.fault)
        self.assertFalse(any(c == 7 and d[0] == 8 for _, c, d in self.drive.attempts))

    def test_heartbeat_flags_trajectory_complete_is_healthy(self):
        self.ready()
        self.assertEqual(self.motor.snapshot()["axis_flags"], 0x80)
        self.assertIsNone(self.motor.fault)

    def test_duplicate_heartbeats_do_not_refresh_stale_feedback(self):
        self.run_motor()
        self.drive.drop.add(1)
        snapshot = self.motor.snapshot()
        data = struct.pack("<IBBBB", 0, 8, 0x80, 0, self.drive.life)
        for _ in range(65):
            self.drive.rx.append((0x21, data))
            self.step()
        self.assertIn("stale", self.motor.fault)
        self.assertGreater(self.motor.snapshot()["diagnostics"]["heartbeat_duplicates"], 50)

    def test_minimum_positive_speed_still_has_stall_supervision(self):
        self.run_motor(5)
        self.drive.stall, self.drive.rps = True, 0.0
        self.until(lambda: self.motor.fault is not None)
        self.assertIn("stalled", self.motor.fault)


if __name__ == "__main__":
    unittest.main()
