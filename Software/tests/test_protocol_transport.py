"""Host protocol checks and MCP2515 SPI/CAN transport behavior checks."""

from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_api import protocol as P
from grinder_api.circuitpython import Registers, Transport, TransportError, open_transport


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self):
        self.now += 0.0001
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds


class Pin:
    def __init__(self):
        self.value = True
        self.closed = 0

    def switch_to_output(self, value):
        self.value = value

    def deinit(self):
        self.closed += 1


class SPI:
    def __init__(self, cs):
        self.cs = cs
        self.regs = bytearray(256)
        self.address = 0
        self.locked = False
        self.busy = False
        self.stuck = None
        self.read_error = False
        self.allow_abort = True
        self.closed = 0
        self.reads = []

    def try_lock(self):
        if self.busy:
            return False
        self.locked = True
        return True

    def configure(self, **options):
        assert options == {"baudrate": 1_000_000, "polarity": 0, "phase": 0}

    def unlock(self):
        self.locked = False

    def write(self, command):
        assert self.locked and not self.cs.value
        if command[0] == 0x03:
            self.address = command[1]
        elif command[0] == 0xC0:
            self.regs[:] = bytes(256)
            self.regs[0x0E], self.regs[0x0F] = 0x80, 0x87
        elif command[0] == 0x05:
            _, address, mask, value = command
            if address in (0x30, 0x40, 0x50):
                mask &= ~0x70  # ABTF, MLOA and TXERR are read-only.
            self.regs[address] = (self.regs[address] & ~mask) | (value & mask)
            if address == 0x0F:
                self.regs[0x0E] = self.regs[0x0F] & 0xE0
                if self.regs[0x0F] & 0x10 and self.allow_abort:
                    for control in (0x30, 0x40, 0x50):
                        if self.regs[control] & 8:
                            self.regs[control] = (self.regs[control] & ~8) | 0x40
        else:
            raise AssertionError("Unexpected diagnostic SPI command")

    def readinto(self, target):
        assert self.locked and not self.cs.value
        self.reads.append((self.address, len(target)))
        if self.read_error:
            raise OSError("injected SPI read failure")
        for index in range(len(target)):
            target[index] = (self.stuck if self.stuck is not None else
                             self.regs[self.address + index])

    def deinit(self):
        self.closed += 1


def message(identifier, data, extended=False):
    return SimpleNamespace(id=identifier, data=bytes(data), extended=extended)


class CAN:
    def __init__(self, spi):
        self.spi = spi
        self.frames = []
        self.sent = []
        self.read_calls = 0
        self.send_error = None
        self.queue_result = True

    def read_message(self):
        self.read_calls += 1
        return self.frames.pop(0) if self.frames else None

    def send(self, frame):
        self.sent.append(frame)
        if self.send_error:
            raise self.send_error
        if self.queue_result:
            self.spi.regs[0x30] = 8
        return self.queue_result


class ProtocolTests(unittest.TestCase):
    def test_exact_revision_endpoint_mapping_and_little_endian_frames(self):
        self.assertEqual(P.FIRMWARE_VERSION, (0, 5, 14))
        self.assertEqual(P.FIELDS["watchdog_timeout"], (150, "f"))
        self.assertEqual(P.FIELDS["watchdog_enabled"], (151, "?"))
        self.assertEqual(P.FIELDS["iq_rate_ms"], (186, "I"))
        self.assertEqual(P.command_id(1, P.RX_SDO), 0x24)
        self.assertEqual(P.read_request("watchdog_timeout"), b"\0\x96\0\0\0\0\0\0")
        self.assertEqual(P.write_request("watchdog_enabled", True), b"\x01\x97\0\0\x01\0\0\0")
        reply = b"\0\x96\0\0" + struct.pack("<f", 0.5)
        self.assertEqual(P.read_reply("watchdog_timeout", reply), 0.5)
        self.assertEqual(P.read_reply("fw_revision", b"\0\x0B\0\0\x0E\xAB\xCD\xEF"), 14)

    def test_reply_echo_boolean_finiteness_and_length_are_validated(self):
        malformed = (b"\0" * 7, b"\x01\x97\0\0\x01\0\0\0",
                     b"\0\x96\0\0\x01\0\0\0", b"\0\x97\0\x01\x01\0\0\0",
                     b"\0\x97\0\0\x02\0\0\0")
        for data in malformed:
            with self.subTest(data=data), self.assertRaises(ValueError):
                P.read_reply("watchdog_enabled", data)
        with self.assertRaises(ValueError):
            P.read_reply("voltage", b"\0\x02\0\0" + struct.pack("<f", float("nan")))

    def test_writes_reject_readonly_unknown_nonfinite_and_invalid_typed_values(self):
        for name, value in (("fw_minor", 5), ("unknown", 0), ("watchdog_timeout", float("inf")),
                            ("watchdog_timeout", 1e100), ("watchdog_enabled", 2),
                            ("iq_rate_ms", -1), ("iq_rate_ms", 2**32), ("control_mode", 256)):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                P.write_request(name, value)
        for node in (0, 64, True, 1.0):
            with self.subTest(node=node), self.assertRaises(ValueError):
                P.command_id(node, 1)

    def test_telemetry_decodes_signed_values_and_output_rpm(self):
        self.assertEqual(P.decode_telemetry(P.HEARTBEAT, struct.pack("<IBBH", 0x800, 1, 8, 33)),
                         (0x800, 1, 8))
        self.assertEqual(P.decode_telemetry(P.IQ, struct.pack("<ff", -0.5, 0.25)), (-0.5, 0.25))
        self.assertAlmostEqual(P.rotor_velocity(110), 14.6666666667)
        self.assertAlmostEqual(P.output_rpm(P.rotor_velocity(110)), 110)
        with self.assertRaises(ValueError):
            P.decode_telemetry(P.ENCODER, struct.pack("<ff", 0, float("nan")))
        for ratio in (0, -1, float("inf")):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                P.output_rpm(1, ratio)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.cs = Clock(), Pin()
        self.spi = SPI(self.cs)
        self.spi.regs[0x0F] = 0x0F
        self.spi.regs[0x28:0x2B] = bytes((0x86, 0xF0, 0))
        self.can = CAN(self.spi)
        self.registers = Registers(self.spi, self.cs, self.clock)
        self.transport = Transport(self.can, message, self.registers, clock=self.clock,
                                   resources=(self.spi, self.cs))

    def test_only_one_pending_send_then_completion_allows_next_frame(self):
        self.assertTrue(self.transport.send(0x24, P.read_request("voltage")))
        self.assertTrue(self.transport.pending)
        self.assertFalse(self.transport.send(0x24, P.read_request("voltage")))
        self.assertEqual(len(self.can.sent), 1)
        self.spi.regs[0x30] = 0
        self.transport.update()
        self.assertFalse(self.transport.pending)
        self.assertTrue(self.transport.send(0x24, P.read_request("voltage")))
        self.assertEqual(self.clock.sleeps, 0)

    def test_invalid_deadline_cannot_disable_tx_timeout(self):
        for timeout in (float("nan"), float("inf"), 0, -0.01, 0.2):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                Transport(self.can, message, self.registers, clock=self.clock, tx_timeout=timeout)

    def test_send_never_drains_received_fault_frames_before_controller_supervision(self):
        fault = message(0x21, struct.pack("<IBBH", 0x100, 1, 0, 1))
        self.can.frames = [fault]
        self.assertTrue(self.transport.send(0x2D, struct.pack("<ff", 1.0, 0)))
        self.assertEqual(self.can.read_calls, 0)
        self.assertEqual(self.can.frames, [fault])
        self.transport.update()
        self.assertEqual(self.transport.receive(), (0x21, fault.data))

    def test_tx_deadline_aborts_hardware_and_raises_clear_error(self):
        self.transport.send(0x24, bytes(8))
        self.clock.now += 0.026
        with self.assertRaisesRegex(TransportError, "deadline"):
            self.transport.update()
        self.assertFalse(self.transport.pending)
        self.assertEqual(self.spi.regs[0x30] & 8, 0)
        self.assertEqual(self.transport.tx_failures, 1)

    def test_tx_failure_flags_and_queue_failure_are_not_silently_accepted(self):
        for error in (0x10, 0x20, 0x40):
            with self.subTest(error=error):
                self.transport.send(0x24, bytes(8))
                self.spi.regs[0x30] = error
                with self.assertRaisesRegex(TransportError, "transmission failed"):
                    self.transport.update()
                self.assertEqual(self.spi.regs[0x30] & 0x70, error)
        self.can.queue_result = False
        with self.assertRaisesRegex(TransportError, "not queued"):
            self.transport.send(0x24, bytes(8))

    def test_readonly_abort_flag_does_not_poison_next_transmission(self):
        self.transport.send(0x24, bytes(8))
        self.transport.abort()
        self.assertEqual(self.spi.regs[0x30] & 0x70, 0x40)
        self.assertTrue(self.transport.send(0x24, bytes(8)))
        self.assertEqual(self.spi.regs[0x30], 8)

    def test_send_exception_aborts_and_invalid_frame_never_reaches_can(self):
        for identifier, payload in ((0x44, bytes(8)), (0x824, bytes(8)), (0x24, bytes(9))):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.transport.send(identifier, payload)
        self.assertEqual(self.can.sent, [])
        self.can.send_error = OSError("injected send error")
        with self.assertRaisesRegex(TransportError, "injected send error"):
            self.transport.send(0x24, bytes(8))
        self.assertFalse(self.transport.pending)

    def test_bounded_receive_filters_other_nodes_extended_and_remote_frames(self):
        self.can.frames = [message(0x41, bytes(8)), message(0x21, bytes(8), True),
                           SimpleNamespace(id=0x21, extended=False, length=8)]
        self.can.frames.extend(message(0x29, struct.pack("<ff", i, 1)) for i in range(100))
        self.transport.update()
        self.assertEqual(self.can.read_calls, 32)
        self.assertEqual(self.transport.rx_filtered, 3)
        received = []
        while (frame := self.transport.receive()) is not None:
            received.append(frame)
        self.assertEqual(len(received), 29)
        self.assertEqual(received[0], (0x29, struct.pack("<ff", 0, 1)))

    def test_rx_is_drained_before_and_after_periodic_register_checks(self):
        self.can.frames = [message(0x29, bytes(8))]
        original_read = self.registers.read
        injected = False

        def read_with_arrival(address, length=1):
            nonlocal injected
            if address == 0x2D and not injected:
                self.assertEqual(self.can.frames, [])
                self.can.frames.append(message(0x21, bytes(8)))
                injected = True
            return original_read(address, length)

        self.registers.read = read_with_arrival
        self.transport.update()
        self.assertEqual(self.transport.receive()[0], 0x29)
        self.assertEqual(self.transport.receive()[0], 0x21)

    def test_idle_diagnostics_are_amortized_but_pending_deadlines_are_not(self):
        self.transport.update()
        self.spi.reads = []
        for _ in range(10):
            self.transport.update()
        self.assertEqual(self.spi.reads, [])
        self.clock.now += 0.02
        self.transport.update()
        self.assertEqual([address for address, _ in self.spi.reads], [0x2D, 0x0E, 0x30, 0x40, 0x50])
        self.transport.send(0x24, bytes(8))
        self.transport._next_diagnostics = self.clock.now + 1
        self.clock.now += 0.026
        with self.assertRaisesRegex(TransportError, "deadline"):
            self.transport.update()

    def test_send_still_checks_mode_before_queuing_inside_diagnostic_interval(self):
        self.transport.update()
        self.spi.regs[0x0E] = self.spi.regs[0x0F] = 0x60
        with self.assertRaisesRegex(TransportError, "mode was lost"):
            self.transport.send(0x24, bytes(8))
        self.assertEqual(self.can.sent, [])

    def test_hardware_overflow_is_recorded_and_cleared_and_software_queue_is_bounded(self):
        self.spi.regs[0x2D] = 0xC0
        self.transport.update()
        self.assertEqual(self.transport.rx_overflows, 2)
        self.assertEqual(self.spi.regs[0x2D], 0)
        self.transport.update()
        self.assertEqual(self.transport.rx_overflows, 2)
        self.can.frames = [message(0x29, bytes(8)) for _ in range(96)]
        for _ in range(3):
            self.transport.update()
        self.assertEqual(self.transport.rx_overflows, 34)
        self.assertEqual(len(self.transport._rx), 64)

    def test_close_aborts_restores_listen_only_and_deinitializes_once(self):
        self.transport.send(0x24, bytes(8))
        self.transport.close()
        self.assertFalse(self.transport.pending)
        self.assertEqual(self.spi.regs[0x0E] & 0xE0, 0x60)
        self.assertEqual((self.spi.closed, self.cs.closed), (1, 1))
        self.transport.close()
        self.assertEqual((self.spi.closed, self.cs.closed), (1, 1))

    def test_failed_abort_does_not_skip_listen_only_close(self):
        self.transport.send(0x24, bytes(8))
        self.spi.allow_abort = False
        with self.assertRaisesRegex(TransportError, "abort timed out"):
            self.transport.close()
        self.assertEqual(self.spi.regs[0x0E] & 0xE0, 0x60)
        self.assertEqual((self.spi.closed, self.cs.closed), (1, 1))

    def test_register_spi_failures_release_cs_lock_and_busy_is_immediate(self):
        self.spi.read_error = True
        with self.assertRaises(OSError):
            self.registers.read(0x0E)
        self.assertTrue(self.cs.value)
        self.assertFalse(self.spi.locked)
        self.spi.busy = True
        with self.assertRaisesRegex(TransportError, "busy"):
            self.registers.read(0x0E)
        self.assertEqual(self.clock.sleeps, 0)

    def factory_modules(self):
        self.created = []
        self.pins = {}

        def spi_factory(**pins):
            self.pins.update(pins)
            return self.spi

        def cs_factory(pin):
            self.assertEqual(pin, 5)
            return self.cs

        def can_factory(spi, cs, **options):
            self.assertEqual(options["baudrate"], 500_000)
            self.assertEqual(options["crystal_freq"], 16_000_000)
            self.assertFalse(options["loopback"])
            # Mimic SPIDevice's documented default request. The proxy must
            # configure the physical SPI at the already-verified 1 MHz rate.
            self.assertTrue(spi.try_lock())
            try:
                spi.configure(baudrate=100_000, polarity=0, phase=0)
            finally:
                spi.unlock()
            spi.regs[0x0E] = 0x60 if options["silent"] else 0
            spi.regs[0x0F] = spi.regs[0x0E] | 7
            spi.regs[0x28:0x2B] = bytes((0x86, 0xF0, 0))
            can = CAN(spi)
            self.created.append((options, can))
            return can

        package = ModuleType("adafruit_mcp2515")
        package.MCP2515 = can_factory
        canio = ModuleType("adafruit_mcp2515.canio")
        canio.Message = message
        return {"board": SimpleNamespace(GP4=4, GP5=5, GP6=6, GP7=7),
                "busio": SimpleNamespace(SPI=spi_factory),
                "digitalio": SimpleNamespace(DigitalInOut=cs_factory),
                "adafruit_mcp2515": package, "adafruit_mcp2515.canio": canio}

    def test_factory_checks_waveshare_pins_silent_before_normal_and_sends_nothing(self):
        with patch.dict(sys.modules, self.factory_modules()):
            transport = open_transport(clock=self.clock)
        self.assertEqual(self.pins, {"clock": 6, "MOSI": 7, "MISO": 4})
        self.assertEqual([options["silent"] for options, _ in self.created], [True, False])
        self.assertTrue(all(not can.sent for _, can in self.created))
        self.assertEqual(self.spi.regs[0x0F] & 0xE8, 8)
        transport.close()

    def test_floating_miso_refuses_factory_before_library_and_releases_resources(self):
        for stuck in (0, 0xFF):
            with self.subTest(stuck=stuck):
                self.setUp()
                self.spi.stuck = stuck
                with patch.dict(sys.modules, self.factory_modules()):
                    with self.assertRaisesRegex(TransportError, "reset signature"):
                        open_transport(clock=self.clock)
                self.assertEqual(self.created, [])
                self.assertEqual((self.spi.closed, self.cs.closed), (1, 1))


if __name__ == "__main__":
    unittest.main()
