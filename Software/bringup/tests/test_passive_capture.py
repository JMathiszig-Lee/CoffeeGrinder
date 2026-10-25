"""Host checks for failed SPI, failed mode checks and a continuously busy bus."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


SPEC = importlib.util.spec_from_file_location(
    "passive_bringup", Path(__file__).resolve().parents[1] / "code.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        # Processing takes time even when every receive call returns a frame.
        self.now += 0.0001
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SPI:
    """Register interface fixture; rejects every unsupported SPI opcode."""

    def __init__(self, cs, stuck=None, fail=None):
        self.cs = cs
        self.stuck = stuck
        self.fail = fail
        self.locked = False
        self.regs = bytearray(256)
        self.address = None
        self.commands = []

    def try_lock(self):
        self.locked = True
        return True

    def unlock(self):
        self.locked = False

    def configure(self, **settings):
        if self.fail == "configure":
            raise OSError("SPI configure failed")

    def write(self, command):
        assert self.locked and not self.cs.value
        self.commands.append(bytes(command))
        if self.fail == "write":
            raise OSError("SPI write failed")
        if command[0] == 0x03:
            self.address = command[1]
        elif command[0] == 0xC0:
            self.regs[:] = bytes(256)
            self.regs[APP.CANSTAT] = 0x80
            self.regs[APP.CANCTRL] = 0x87
        elif command[0] == 0x05:
            # The application has only one permitted state-changing request.
            assert bytes(command) == bytes((0x05, APP.CANCTRL, 0xE0, 0x60))
            self.regs[APP.CANSTAT] = 0x60
            self.regs[APP.CANCTRL] = 0x67
        else:
            raise AssertionError("Unexpected SPI opcode: %r" % command)

    def readinto(self, target):
        assert self.locked and not self.cs.value
        if self.fail == "read":
            raise OSError("SPI read failed")
        for index in range(len(target)):
            target[index] = (self.stuck if self.stuck is not None else
                             self.regs[self.address + index])


class Bus:
    def __init__(self, frames=(), failure=None):
        self.frames = frames
        self.failure = failure
        self.reads = 0

    def read_message(self):
        self.reads += 1
        if self.failure:
            raise self.failure
        if self.frames:
            return self.frames[(self.reads - 1) % len(self.frames)]
        return None


class PassiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.cs = SimpleNamespace(value=True)
        self.spi = SPI(self.cs)
        self.clock = Clock()
        self.output = []
        self.constructed = 0
        self.bus = Bus()

    def factory(self, spi, cs, **options):
        self.constructed += 1
        self.assertEqual(options, {
            "silent": True, "loopback": False,
            "baudrate": 500_000, "crystal_freq": 16_000_000})
        spi.regs[APP.CANSTAT] = 0x60
        spi.regs[APP.CANCTRL] = 0x67
        spi.regs[0x28:0x2B] = bytes((0x86, 0xF0, 0x00))
        return self.bus

    def run_session(self, factory=None):
        return APP.run_session(self.spi, self.cs, factory or self.factory,
                               self.clock, self.output.append)

    def test_floating_miso_is_rejected_before_library_initialization(self):
        for stuck in (0x00, 0xFF):
            with self.subTest(stuck=stuck):
                self.spi = SPI(self.cs, stuck=stuck)
                with self.assertRaisesRegex(RuntimeError, "reset signature"):
                    self.run_session()
                self.assertEqual(self.constructed, 0)
                self.assertEqual(self.bus.reads, 0)
                self.assertTrue(self.cs.value)
                self.assertFalse(self.spi.locked)

    def test_spi_failures_release_cs_and_lock(self):
        for failure in ("configure", "write", "read"):
            with self.subTest(failure=failure):
                self.spi = SPI(self.cs, fail=failure)
                with self.assertRaises(OSError):
                    self.run_session()
                self.assertEqual(self.constructed, 0)
                self.assertTrue(self.cs.value)
                self.assertFalse(self.spi.locked)

    def test_mismatched_mode_or_timing_refuses_capture_and_requests_listen_only(self):
        for register, value in ((APP.CANSTAT, 0x00), (APP.CANCTRL, 0x00),
                                (APP.CNF3, 0x00)):
            with self.subTest(register=register):
                self.spi = SPI(self.cs)

                def incorrect_factory(spi, cs, **options):
                    result = self.factory(spi, cs, **options)
                    spi.regs[register] = value
                    return result

                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    self.run_session(incorrect_factory)
                self.assertEqual(self.bus.reads, 0)
                self.assertEqual(self.spi.regs[APP.CANSTAT] & 0xE0, 0x60)

    def test_receive_failure_preserves_listen_only_and_does_not_retry(self):
        self.bus = Bus(failure=OSError("receive failed"))
        with self.assertRaisesRegex(OSError, "receive failed"):
            self.run_session()
        self.assertEqual(self.constructed, 1)
        self.assertEqual(self.bus.reads, 1)
        self.assertEqual(self.spi.regs[APP.CANSTAT] & 0xE0, 0x60)

    def test_continuously_busy_bus_finishes_and_logs_std_extended_and_rtr(self):
        self.bus = Bus(frames=(
            SimpleNamespace(id=0x123, extended=False, data=b"\x01\xAB"),
            SimpleNamespace(id=0x1234567, extended=True, data=b"\xFF"),
            SimpleNamespace(id=0x321, extended=False, length=8),
            SimpleNamespace(id=0x1234568, extended=True, length=4),
        ))
        result = self.run_session()
        self.assertGreaterEqual(self.clock.now, APP.CAPTURE_SECONDS)
        self.assertLess(self.clock.now, APP.CAPTURE_SECONDS + 0.2)
        self.assertGreater(result["total"], 1000)
        self.assertLess(self.bus.reads, 100_000)
        self.assertEqual(result["standard"] + result["extended"], result["total"])
        self.assertGreater(result["rtr"], 0)
        self.assertLessEqual(result["logged"], APP.MAX_FRAME_LINES)
        self.assertTrue(any("STD ID=" in line for line in self.output))
        self.assertTrue(any("EXT ID=" in line for line in self.output))
        self.assertTrue(any("RTR DLC=" in line for line in self.output))
        self.assertEqual(self.constructed, 1)

    def test_silent_bus_reports_no_frames_and_finishes(self):
        result = self.run_session()
        self.assertEqual(result["total"], 0)
        self.assertTrue(any("No frames observed" in line for line in self.output))
        self.assertLess(self.clock.now, APP.CAPTURE_SECONDS + 0.2)


if __name__ == "__main__":
    unittest.main()
