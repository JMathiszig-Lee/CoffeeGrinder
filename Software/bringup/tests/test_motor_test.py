"""Behavioral bench simulator: watchdog, command limits and failure cleanup."""

import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest


SPEC = importlib.util.spec_from_file_location(
    "motor_trial", Path(__file__).resolve().parents[1] / "motor_test.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


def message(identifier, data, extended=False):
    return SimpleNamespace(id=identifier, data=bytes(data), extended=extended)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.00005
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class MotorBus:
    """CAN Simple peer with periodic telemetry and a drive-side watchdog.

    It models a watchdog fed by every directed frame, so polling during the
    expiry experiment cannot accidentally count as a successful proof.
    """

    def __init__(self, clock):
        self.clock = clock
        self.values = {
            9: 0, 10: 5, 11: 14, 12: 0, 2: 24.2, 192: True, 330: True,
            289: 12.0, 246: 6.0, 150: 0.0, 151: False,
            283: 3, 284: 1, 267: 0.0, 268: 0.0,
            147: False, 149: False, 279: True, 282: True,
        }
        self.formats = {endpoint: fmt for endpoint, fmt in APP.FIELDS.values()}
        self.pending = []
        self.attempts = []
        self.state = 1
        self.error = 0
        self.flags = 0
        self.position = 0.0
        self.velocity = 0.0
        self.last_update = 0.0
        self.last_feed = 0.0
        self.next_heartbeat = 0.0
        self.next_encoder = 0.0
        self.counter = 0
        self.watchdog_expiries = []
        self.clear_events = []
        self.activation_events = []
        self.nonzero_events = []
        self.watchdog_works = True
        self.closed_loop_disarms = True
        self.watchdog_extra_error = 0
        self.encoder_enabled = True
        self.drop_encoder_after_motion = False
        self.fail_on_nonzero = False
        self.fail_next_zero = False
        self.motion_fault = 0
        self.motion_fault_time = None
        self.limits_written = False
        self.readback_overrides = {}

    def advance(self):
        now = self.clock.now
        self.position += self.velocity * (now - self.last_update)
        self.last_update = now
        if self.motion_fault and self.nonzero_events and self.motion_fault_time is None:
            if now - self.nonzero_events[0] > 0.1:
                self.error |= self.motion_fault
                self.state, self.velocity = 1, 0.0
                self.motion_fault_time = now
        if self.watchdog_works and self.values[151] and self.values[150] > 0:
            if now - self.last_feed > self.values[150] and not self.error & 0x800:
                self.error |= 0x800 | self.watchdog_extra_error
                if self.state != 8 or self.closed_loop_disarms:
                    self.state = 1
                self.velocity = 0.0
                self.watchdog_expiries.append((now, self.error))

    def read_message(self):
        self.advance()
        if self.pending:
            return self.pending.pop(0)
        now = self.clock.now
        if now >= self.next_heartbeat:
            self.next_heartbeat = now + 0.1
            self.counter = (self.counter + 1) & 0xFFFF
            return message(0x21, struct.pack("<IBBH", self.error, self.state,
                                             self.flags, self.counter))
        if self.drop_encoder_after_motion and self.nonzero_events:
            self.encoder_enabled = False
        if self.encoder_enabled and now >= self.next_encoder:
            self.next_encoder = now + 0.01
            return message(0x29, struct.pack("<ff", self.position, self.velocity))
        return None

    def send(self, frame):
        self.advance()
        now = self.clock.now
        self.attempts.append((now, frame))
        assert frame.extended is False and frame.id >> 5 == 1
        command = frame.id & 0x1F
        data = frame.data
        assert len(data) == 8
        # This simulator accepts only the documented bench command surface.
        assert command in (0x04, 0x05, 0x07, 0x0B, 0x0D, 0x0F, 0x18)
        if command == 0x0D:
            velocity, torque = struct.unpack("<ff", data)
            assert abs(velocity) <= 2 and torque == 0
            if velocity and self.fail_on_nonzero:
                self.fail_on_nonzero = False
                raise OSError("injected nonzero TX failure")
            if not velocity and self.fail_next_zero:
                self.fail_next_zero = False
                raise OSError("injected zero TX failure")
            self.values[267], self.values[268] = velocity, torque
            if velocity:
                self.nonzero_events.append(now)
            self.velocity = velocity if self.state == 8 else 0.0
        elif command in (0x04, 0x05):
            opcode, endpoint, reserved = struct.unpack("<BHB", data[:4])
            assert reserved == 0 and endpoint in self.formats
            if command == 0x04:
                assert opcode == 0 and data[4:] == b"\0" * 4
                value = self.values[endpoint]
                if self.limits_written:
                    value = self.readback_overrides.get(endpoint, value)
                codec = "B" if self.formats[endpoint] == "?" else self.formats[endpoint]
                raw = struct.pack("<" + codec, value)
                self.pending.append(message(0x24, data[:4] + raw + bytes(4 - len(raw))))
            else:
                assert opcode == 1 and endpoint in (150, 151, 279, 282)
                if endpoint in (279, 282):
                    assert data[4:] == b"\x01\0\0\0"
                codec = "B" if self.formats[endpoint] == "?" else self.formats[endpoint]
                fmt = "<" + codec
                self.values[endpoint] = struct.unpack(fmt, data[4:4 + struct.calcsize(fmt)])[0]
        elif command == 0x07:
            requested = struct.unpack("<I", data[:4])[0]
            assert requested in (1, 8)
            self.state = requested
            self.velocity = self.values[267] if requested == 8 else 0.0
            if requested == 8:
                self.activation_events.append(now)
        elif command == 0x0B:
            control, input_mode = struct.unpack("<II", data)
            assert (control, input_mode) == (2, 1)
            self.values[283], self.values[284] = control, input_mode
        elif command == 0x0F:
            velocity_limit, current_limit = struct.unpack("<ff", data)
            assert 0 < velocity_limit <= 2 and 0 < current_limit <= 1
            self.values[289], self.values[246] = velocity_limit, current_limit
            self.limits_written = True
        elif command == 0x18:
            self.clear_events.append((now, self.error))
            self.error = 0
        self.last_feed = now


class MotorTrialTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bus = MotorBus(self.clock)
        self.output = []
        self.trial = APP.MotorTrial(self.bus, message, self.clock, self.output.append)

    def commands(self):
        return [(when, frame.id & 0x1F, frame.data) for when, frame in self.bus.attempts]

    def test_inspect_does_not_mutate_or_activate(self):
        self.trial.run(move=False)
        self.assertTrue(self.bus.attempts)
        self.assertTrue(all(command == 0x04 for _, command, _ in self.commands()))
        self.assertEqual(self.bus.activation_events, [])

    def test_motion_waits_for_watchdog_proof_and_stays_within_limits(self):
        self.trial.run(move=True)
        self.assertTrue(self.bus.watchdog_expiries)
        self.assertTrue(self.bus.activation_events)
        self.assertLess(self.bus.watchdog_expiries[0][0], self.bus.activation_events[0])
        self.assertEqual([error for _, error in self.bus.clear_events], [0x800, 0x800])
        self.assertTrue(self.bus.nonzero_events)
        self.assertEqual(len(self.bus.watchdog_expiries), 2)
        self.assertLess(self.bus.watchdog_expiries[1][0], self.bus.nonzero_events[0])
        self.assertLess(self.bus.nonzero_events[-1] - self.bus.nonzero_events[0], 3.1)
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.velocity, 0)
        self.assertEqual(self.bus.values[267], 0)
        self.assertLessEqual(self.bus.values[289], 2)
        self.assertLessEqual(self.bus.values[246], 1)

    def test_no_activation_when_watchdog_never_expires(self):
        self.bus.watchdog_works = False
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            self.trial.run(move=True)
        self.assertEqual(self.bus.activation_events, [])
        self.assertEqual(self.bus.nonzero_events, [])

    def test_unexpected_fault_is_never_cleared(self):
        self.bus.watchdog_extra_error = 0x100
        with self.assertRaisesRegex(RuntimeError, "Drive fault"):
            self.trial.run(move=True)
        self.assertEqual(self.bus.clear_events, [])
        self.assertEqual(self.bus.activation_events, [])

    def test_stale_encoder_feedback_aborts_and_stops(self):
        self.bus.drop_encoder_after_motion = True
        with self.assertRaisesRegex(RuntimeError, "feedback timeout"):
            self.trial.run(move=True)
        self.assertTrue(self.bus.nonzero_events)
        self.assertLess(self.clock.now - self.bus.nonzero_events[0], 2)
        self.assertEqual(self.bus.state, 1)
        self.assertEqual(self.bus.values[267], 0)

    def test_cleanup_attempts_idle_even_when_zero_transmit_fails(self):
        self.bus.fail_on_nonzero = True
        original_send = self.bus.send

        def send_with_cleanup_failure(frame):
            try:
                original_send(frame)
            except OSError:
                self.bus.fail_next_zero = True
                raise

        self.bus.send = send_with_cleanup_failure
        with self.assertRaisesRegex((RuntimeError, OSError), "failure"):
            self.trial.run(move=True)
        failed_target_index = next(index for index, (_, command, data) in enumerate(
            self.commands()) if command == 0x0D and struct.unpack("<f", data[:4])[0])
        after_failure = self.commands()[failed_target_index + 1:]
        self.assertTrue(any(command == 0x0D and data == b"\0" * 8
                            for _, command, data in after_failure))
        self.assertTrue(any(command == 0x07 and struct.unpack("<I", data[:4])[0] == 1
                            for _, command, data in after_failure))
        self.assertEqual(self.bus.state, 1)

    def test_closed_loop_is_refused_without_both_proofs(self):
        for watchdog, limits in ((False, False), (True, False), (False, True)):
            with self.subTest(watchdog=watchdog, limits=limits):
                self.trial.watchdog_proven = watchdog
                self.trial.limits_verified = limits
                with self.assertRaisesRegex(RuntimeError, "refused"):
                    self.trial.axis_state(8)
                self.assertEqual(self.bus.attempts, [])

    def test_invalid_targets_are_rejected_before_transmission(self):
        self.trial.watchdog_proven = self.trial.limits_verified = True
        self.trial.state = 8
        for target in (float("nan"), float("inf"), float("-inf"), 3.0, -3.0):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    self.trial.setpoint(target)
                self.assertEqual(self.bus.attempts, [])

    def test_write_and_command_allowlists_reject_unsupported_mutations(self):
        for name in ("current_limit", "input_velocity", "fw_minor", "voltage"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.trial.write(name, 100)
        for command in (0x06, 0x0C, 0x0E, 0x16, 0x1F):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    self.trial._send(command, bytes(8))
        for state in (0, 3, 4, 7):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    self.trial.axis_state(state)
        for name in ("enable_vel_limit", "enable_overspeed_error"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.trial.write(name, False)
        self.assertEqual(self.bus.attempts, [])

    def test_initial_unexpected_fault_is_not_cleared_or_mutated(self):
        self.bus.error = 0x100
        with self.assertRaisesRegex(RuntimeError, "Drive fault"):
            self.trial.run(move=True)
        self.assertEqual(self.bus.attempts, [])
        self.assertEqual(self.bus.clear_events, [])

    def test_first_mutation_transmit_failure_still_attempts_zero_and_idle(self):
        original_send = self.bus.send
        failed = False

        def send_with_initial_failure(frame):
            nonlocal failed
            if frame.id & 0x1F == 0x0F and not failed:
                failed = True
                self.bus.attempts.append((self.clock.now, frame))
                raise OSError("initial mutation failed")
            original_send(frame)

        self.bus.send = send_with_initial_failure
        with self.assertRaisesRegex(OSError, "initial mutation failed"):
            self.trial.run(move=True)
        commands = self.commands()
        failed_index = next(index for index, (_, command, _) in enumerate(commands)
                            if command == 0x0F)
        self.assertTrue(any(command == 0x0D for _, command, _ in commands[failed_index + 1:]))
        self.assertTrue(any(command == 0x07 for _, command, _ in commands[failed_index + 1:]))
        self.assertEqual(self.bus.activation_events, [])

    def test_abort_callback_failure_does_not_prevent_zero_and_idle_attempts(self):
        def failed_abort():
            raise RuntimeError("injected abort failure")

        self.trial.abort_tx = failed_abort
        with self.assertRaisesRegex(RuntimeError, "abort failure"):
            self.trial.run(move=True)
        self.assertTrue(self.bus.nonzero_events)
        last_target_time = self.bus.nonzero_events[-1]
        self.assertTrue(any(when > last_target_time and command == 0x0D and data == bytes(8)
                            for when, command, data in self.commands()))
        self.assertTrue(any(when > last_target_time and command == 0x07
                            and struct.unpack("<I", data[:4])[0] == 1
                            for when, command, data in self.commands()))
        self.assertEqual(self.bus.state, 1)

    def test_fault_during_motion_attempts_stop_and_never_clears_new_fault(self):
        self.bus.motion_fault = 0x100
        with self.assertRaisesRegex(RuntimeError, "Drive fault"):
            self.trial.run(move=True)
        self.assertIsNotNone(self.bus.motion_fault_time)
        self.assertEqual([error for _, error in self.bus.clear_events], [0x800, 0x800])
        self.assertTrue(any(when >= self.bus.motion_fault_time and command == 0x0D
                            and data == bytes(8) for when, command, data in self.commands()))
        self.assertTrue(any(when >= self.bus.motion_fault_time and command == 0x07
                            and struct.unpack("<I", data[:4])[0] == 1
                            for when, command, data in self.commands()))
        self.assertEqual(self.bus.state, 1)

    def test_no_positive_target_when_watchdog_fault_does_not_disarm_closed_loop(self):
        self.bus.closed_loop_disarms = False
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            self.trial.run(move=True)
        self.assertEqual(len(self.bus.activation_events), 1)
        self.assertEqual(self.bus.nonzero_events, [])
        self.assertEqual([error for _, error in self.bus.clear_events], [0x800])
        self.assertEqual(self.bus.state, 1)

    def test_nonfinite_limits_from_inspection_refuse_all_mutations(self):
        for endpoint in (289, 246):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(endpoint=endpoint, value=value):
                    self.setUp()
                    self.bus.values[endpoint] = value
                    with self.assertRaisesRegex(RuntimeError, "Invalid float endpoint"):
                        self.trial.run(move=True)
                    self.assertTrue(all(command == 0x04 for _, command, _ in self.commands()))
                    self.assertEqual(self.bus.activation_events, [])

    def test_nan_setup_readback_refuses_activation_and_attempts_stop(self):
        for endpoint in (289, 246):
            with self.subTest(endpoint=endpoint):
                self.setUp()
                self.bus.readback_overrides[endpoint] = float("nan")
                with self.assertRaisesRegex(RuntimeError, "Invalid float endpoint"):
                    self.trial.run(move=True)
                self.assertTrue(self.bus.limits_written)
                self.assertEqual(self.bus.activation_events, [])
                self.assertEqual(self.bus.nonzero_events, [])
                self.assertTrue(any(command == 0x07 and data[:4] == b"\x01\0\0\0"
                                    for _, command, data in self.commands()))

    def test_stop_requires_new_encoder_before_restoring_watchdog(self):
        self.trial.original = {"watchdog_enabled": False, "watchdog_timeout": 0.0}
        self.trial.hb_seq = self.trial.encoder_seq = 1
        self.trial.last_hb = self.trial.last_encoder = self.clock.now
        self.trial.velocity = 0.0
        self.bus.encoder_enabled = False
        self.bus.values[151] = True
        self.bus.values[150] = 0.5
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            self.trial.stop()
        self.assertTrue(any(command == 0x0D for _, command, _ in self.commands()))
        self.assertTrue(any(command == 0x07 for _, command, _ in self.commands()))
        self.assertFalse(any(command == 0x05 for _, command, _ in self.commands()))
        self.assertTrue(self.bus.values[151])

    def test_disabled_factory_protections_are_enabled_and_read_back_before_activation(self):
        self.bus.values[279] = self.bus.values[282] = False
        self.trial.run(move=True)
        commands = self.commands()
        activation = next(index for index, (_, command, data) in enumerate(commands)
                          if command == 0x07 and data[:4] == b"\x08\0\0\0")
        for endpoint in (279, 282):
            with self.subTest(endpoint=endpoint):
                write = next(index for index, (_, command, data) in enumerate(commands)
                             if command == 0x05 and data[1:3] == struct.pack("<H", endpoint))
                self.assertEqual(commands[write][2][4:], b"\x01\0\0\0")
                self.assertTrue(any(command == 0x04 and data[1:3] == struct.pack("<H", endpoint)
                                    for _, command, data in commands[write + 1:activation]))
                self.assertTrue(self.bus.values[endpoint])

    def test_failed_protection_enable_readback_refuses_activation(self):
        self.bus.values[279] = self.bus.values[282] = False
        self.bus.readback_overrides[279] = False
        with self.assertRaisesRegex(RuntimeError, "Readback mismatch: enable_vel_limit"):
            self.trial.run(move=True)
        self.assertEqual(self.bus.activation_events, [])
        self.assertEqual(self.bus.nonzero_events, [])

    def test_invalid_boolean_endpoint_is_not_treated_as_enabled(self):
        self.bus.values[279] = 2
        with self.assertRaisesRegex(RuntimeError, "Invalid boolean endpoint"):
            self.trial.run(move=True)
        self.assertTrue(all(command == 0x04 for _, command, _ in self.commands()))


if __name__ == "__main__":
    unittest.main()
