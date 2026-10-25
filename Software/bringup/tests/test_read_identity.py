"""Verify the read allowlist, heartbeat gate, reply validation and cleanup."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


SPEC = importlib.util.spec_from_file_location(
    "read_identity", Path(__file__).resolve().parents[1] / "read_identity.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


def message(identifier, data, extended=False):
    return SimpleNamespace(id=identifier, data=bytes(data), extended=extended)


def heartbeat(error=0, state=1, flags=0):
    return message(0x21, error.to_bytes(4, "little") + bytes((state, flags, 9, 10)))


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.0001
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Registers:
    def __init__(self):
        self.mode = 0x60
        self.restores = 0

    def probe(self, emit):
        pass

    def verify(self, emit):
        assert self.mode == 0x60

    def read(self, address, length):
        if address == 0x0E:
            return bytes((self.mode, self.mode | 7))
        if address == 0x28:
            return bytes((0x86, 0xF0, 0))
        raise AssertionError("Unexpected diagnostic register")

    def preserve_listen_only(self, emit):
        self.restores += 1
        self.mode = 0x60


class Bus:
    def __init__(self, fixture):
        self.fixture = fixture
        self.pending = []
        self.sent = []
        self.fallback = heartbeat()
        self.failure = None
        self.reply_builder = self.valid_reply

    def read_message(self):
        return self.pending.pop(0) if self.pending else self.fallback

    def send(self, frame):
        # A bus double enforces the independently documented exact allowlist.
        assert frame.id == 0x24 and frame.extended is False
        assert frame.data[1] in (6, 7, 8, 9, 10, 11, 12)
        assert frame.data == bytes((0, frame.data[1], 0, 0, 0, 0, 0, 0))
        self.sent.append(frame)
        if self.failure:
            raise self.failure
        self.pending.extend(self.reply_builder(frame.data[1]))

    def valid_reply(self, endpoint):
        # Undefined uint8 padding is deliberately nonzero and must be retained.
        value = self.fixture.expected_values[endpoint]
        return [message(0x24, (0, endpoint, 0, 0, value, 0x55, 0xAA, 0x66))]


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.expected_values = {6: 3, 7: 6, 8: 24, 9: 0, 10: 5, 11: 14, 12: 0}
        self.clock = Clock()
        self.registers = Registers()
        self.output = []
        self.options = []
        self.passive = Bus(self)
        self.active = Bus(self)

    def factory(self, spi, cs, **options):
        self.options.append(options)
        self.assertFalse(options["loopback"])
        self.assertEqual(options["baudrate"], 500_000)
        self.assertEqual(options["crystal_freq"], 16_000_000)
        self.registers.mode = 0x60 if options["silent"] else 0
        return self.passive if options["silent"] else self.active

    def run_query(self):
        return APP.run_identity(None, None, self.factory, self.registers,
                                message, self.clock, self.output.append)

    def assert_restored(self):
        self.assertEqual(self.registers.restores, 1)
        self.assertEqual(self.registers.mode, 0x60)
        self.assertLess(self.clock.now, 5.01)

    def test_exact_version_mapping_and_only_seven_allowlisted_reads(self):
        result = self.run_query()
        self.assertEqual(result, {
            "hw_version_major": 3, "hw_version_minor": 6, "hw_version_variant": 24,
            "fw_version_major": 0, "fw_version_minor": 5,
            "fw_version_revision": 14, "fw_version_unreleased": 0})
        self.assertEqual([frame.data[1] for frame in self.active.sent], list(range(6, 13)))
        self.assertEqual(self.passive.sent, [])
        self.assertEqual([options["silent"] for options in self.options], [True, False])
        self.assertTrue(any("55 AA 66" in line for line in self.output))
        self.assert_restored()

    def test_no_valid_safe_heartbeat_means_no_requests_or_normal_mode(self):
        candidates = [None, heartbeat(error=1), heartbeat(state=8), heartbeat(flags=1),
                      heartbeat(flags=8), message(0x21, b"\0" * 7),
                      message(0x41, b"\0" * 4 + b"\x01\0\0\0"),
                      message(0x21, b"\0" * 4 + b"\x01\0\0\0", extended=True),
                      SimpleNamespace(id=0x21, length=8, extended=False)]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.setUp()
                self.passive.fallback = candidate
                with self.assertRaises(RuntimeError):
                    self.run_query()
                self.assertEqual(self.active.sent, [])
                self.assertEqual([option["silent"] for option in self.options], [True])
                self.assert_restored()

    def test_fresh_heartbeat_is_required_after_mode_change(self):
        self.active.fallback = None
        with self.assertRaisesRegex(RuntimeError, "safe heartbeat"):
            self.run_query()
        self.assertEqual(self.active.sent, [])
        self.assert_restored()

    def test_malformed_unrelated_extended_rtr_and_write_replies_are_ignored(self):
        def mixed_replies(endpoint):
            good = self.active.valid_reply(endpoint)[0]
            return [message(0x25, good.data), message(0x24, good.data[:7]),
                    message(0x24, bytes((1,)) + good.data[1:]),
                    message(0x24, bytes((0, endpoint, 1, 0)) + good.data[4:]),
                    message(0x24, bytes((0, endpoint, 0, 1)) + good.data[4:]),
                    message(0x24, good.data, extended=True),
                    SimpleNamespace(id=0x24, length=8, extended=False), good]

        self.active.reply_builder = mixed_replies
        self.assertEqual(len(self.run_query()), 7)
        self.assert_restored()

    def test_missing_reply_times_out_without_retry_or_next_endpoint(self):
        self.active.reply_builder = lambda endpoint: []
        with self.assertRaisesRegex(RuntimeError, "endpoint 6 reply"):
            self.run_query()
        self.assertEqual(len(self.active.sent), 1)
        self.assert_restored()

    def test_send_exception_restores_listen_only(self):
        self.active.failure = OSError("SPI send failed")
        with self.assertRaisesRegex(OSError, "SPI send failed"):
            self.run_query()
        self.assertEqual(len(self.active.sent), 1)
        self.assert_restored()

    def test_fault_heartbeat_during_reply_stops_further_requests(self):
        self.active.reply_builder = lambda endpoint: [heartbeat(error=0x100)]
        with self.assertRaisesRegex(RuntimeError, "not IDLE"):
            self.run_query()
        self.assertEqual(len(self.active.sent), 1)
        self.assert_restored()


if __name__ == "__main__":
    unittest.main()
