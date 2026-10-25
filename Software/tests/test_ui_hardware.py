"""Host checks for display wiring and nonblocking seesaw encoder sampling."""

from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui.hardware import Encoder, open_display, open_encoder


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        raise AssertionError("Normal encoder reads must never sleep")


class I2C:
    def __init__(self):
        self.locked = self.busy = self.fail_read = False
        self.position = -3
        self.pressed = False
        self.command = None
        self.writes = []
        self.closed = 0

    def try_lock(self):
        if self.busy:
            return False
        self.locked = True
        return True

    def unlock(self):
        self.locked = False

    def writeto(self, address, command):
        assert self.locked and address == 0x36
        self.command = bytes(command)
        self.writes.append(self.command)

    def readfrom_into(self, address, target):
        assert self.locked and address == 0x36
        if self.fail_read:
            raise OSError("Encoder disconnected")
        if self.command == b"\x11\x30":
            target[:] = struct.pack(">i", self.position)
        else:
            assert self.command == b"\x01\x04"
            target[:] = struct.pack(">I", 0x00FF00FF | (0 if self.pressed else 1 << 24))

    def deinit(self):
        self.closed += 1


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.i2c = Clock(), I2C()
        self.encoder = Encoder(self.i2c, clock=self.clock)

    def test_signed_position_and_active_low_button_are_read_in_delayed_phases(self):
        self.assertEqual(self.encoder.read(), (0, False))
        self.assertEqual(self.i2c.writes, [b"\x11\x30"])
        self.clock.now = 0.004
        self.assertEqual(self.encoder.read(), (0, False))
        self.assertEqual(len(self.i2c.writes), 1)
        self.clock.now = 0.008
        self.assertEqual(self.encoder.read(), (-3, False))
        self.assertEqual(self.i2c.writes[-1], b"\x01\x04")
        self.i2c.pressed = True
        self.clock.now = 0.016
        self.assertEqual(self.encoder.read(), (-3, True))
        self.assertFalse(self.i2c.locked)

    def test_busy_bus_returns_without_wait_and_times_out_instead_of_faking_input(self):
        self.i2c.busy = True
        self.assertEqual(self.encoder.read(), (0, False))
        self.assertEqual(self.i2c.writes, [])
        self.clock.now = 0.251
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.encoder.read()

    def test_response_delay_starts_after_register_selection_finishes(self):
        write = self.i2c.writeto

        def slow_write(address, command):
            write(address, command)
            self.clock.now += 0.005

        self.i2c.writeto = slow_write
        self.encoder.read()
        self.clock.now = 0.012
        self.assertEqual(self.encoder.read(), (0, False))
        self.assertEqual(len(self.i2c.writes), 1)
        self.clock.now = 0.014
        self.assertEqual(self.encoder.read(), (-3, False))
        self.assertEqual(len(self.i2c.writes), 2)

    def test_long_render_pauses_allow_fresh_position_and_button_reads(self):
        self.clock.now = 1.0
        self.assertEqual(self.encoder.read(), (0, False))
        self.assertEqual(self.i2c.writes, [b"\x11\x30"])
        self.clock.now = 1.4
        self.assertEqual(self.encoder.read(), (-3, False))
        self.i2c.pressed = True
        self.clock.now = 2.0
        self.assertEqual(self.encoder.read(), (-3, True))
        self.assertFalse(self.i2c.locked)

    def test_lock_recovery_after_a_pause_resets_the_busy_deadline(self):
        self.i2c.busy = True
        self.encoder.read()
        self.clock.now = 0.4
        self.i2c.busy = False
        self.assertEqual(self.encoder.read(), (0, False))
        self.clock.now = 0.41
        self.i2c.busy = True
        self.assertEqual(self.encoder.read(), (0, False))
        self.clock.now = 0.65
        self.assertEqual(self.encoder.read(), (0, False))
        self.clock.now = 0.661
        with self.assertRaisesRegex(RuntimeError, "I2C bus lock timeout"):
            self.encoder.read()

    def test_i2c_exception_releases_lock_and_close_is_idempotent(self):
        self.encoder.read()
        self.clock.now = 0.009
        self.i2c.fail_read = True
        with self.assertRaisesRegex(OSError, "disconnected"):
            self.encoder.read()
        self.assertFalse(self.i2c.locked)
        self.encoder.close()
        self.encoder.close()
        self.assertEqual(self.i2c.closed, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.encoder.read()


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.i2c = Clock(), I2C()
        self.pins = {}
        self.product = 4991
        self.chip_missing = False
        self.display_failure = False
        self.releases = 0
        self.spi_closed = 0
        self.pullup = None

    def modules(self):
        def spi_factory(**pins):
            self.pins["spi"] = pins
            return SimpleNamespace(deinit=close_spi)

        def close_spi():
            self.spi_closed += 1

        def i2c_factory(**pins):
            self.pins["i2c"] = pins
            return self.i2c

        def release():
            self.releases += 1

        def fourwire(spi, **options):
            self.pins["fourwire"] = options
            return SimpleNamespace(spi=spi)

        def display_factory(bus, **options):
            if self.display_failure:
                raise RuntimeError("Panel initialization failed")
            return SimpleNamespace(root_group=None, **options)

        outer = self

        class Seesaw:
            INPUT_PULLUP = 2
            chip_id = 0x55

            def __init__(self, i2c, addr):
                if outer.chip_missing:
                    raise OSError("No I2C device at 0x36")
                outer.assertEqual(addr, 0x36)

            def get_version(self):
                return (outer.product << 16) | 7

            def pin_mode(self, pin, mode):
                outer.pullup = pin, mode

            def encoder_position(self):
                return -3

            def digital_read(self, pin):
                outer.assertEqual(pin, 24)
                return True

        seesaw_package = ModuleType("adafruit_seesaw")
        seesaw_module = ModuleType("adafruit_seesaw.seesaw")
        seesaw_module.Seesaw = Seesaw
        return {"board": SimpleNamespace(**{"GP%d" % pin: pin for pin in range(29)}),
                "busio": SimpleNamespace(SPI=spi_factory, I2C=i2c_factory),
                "displayio": SimpleNamespace(release_displays=release),
                "fourwire": SimpleNamespace(FourWire=fourwire),
                "adafruit_st7789": SimpleNamespace(ST7789=display_factory),
                "adafruit_seesaw": seesaw_package, "adafruit_seesaw.seesaw": seesaw_module}

    def test_display_28_uses_verified_spi_pins_landscape_and_native_group_api(self):
        with patch.dict(sys.modules, self.modules()):
            display = open_display()
        self.assertEqual(self.pins["spi"], {"clock": 18, "MOSI": 19})
        self.assertEqual(self.pins["fourwire"], {"command": 16, "chip_select": 17, "baudrate": 24_000_000})
        self.assertEqual((display.width, display.height, display.rotation), (320, 240, 270))
        self.assertEqual(display.backlight_pin, 20)
        self.assertEqual(display.brightness, 0.8)
        self.assertFalse(display.auto_refresh)
        display.root_group = "renderer group"
        self.assertEqual(display.root_group, "renderer group")

    def test_display_failure_releases_bus_resources(self):
        self.display_failure = True
        with patch.dict(sys.modules, self.modules()):
            with self.assertRaisesRegex(RuntimeError, "Panel"):
                open_display()
        self.assertEqual(self.spi_closed, 1)
        self.assertEqual(self.releases, 2)

    def test_encoder_uses_actual_qwst_gp4_gp5_and_probes_firmware_pullup(self):
        with patch.dict(sys.modules, self.modules()):
            encoder = open_encoder(clock=self.clock)
        self.assertEqual(self.pins["i2c"], {"scl": 5, "sda": 4, "frequency": 100_000})
        self.assertEqual(self.pullup, (24, 2))
        self.assertEqual(encoder.read(), (-3, False))
        self.assertEqual(encoder.chip_id, 0x55)
        self.assertEqual(encoder.firmware_version >> 16, 4991)

    def test_missing_or_wrong_encoder_is_reported_and_bus_is_released(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.setUp()
                self.chip_missing = missing
                self.product = 1234
                with patch.dict(sys.modules, self.modules()):
                    with self.assertRaises((OSError, RuntimeError)):
                        open_encoder(clock=self.clock)
                self.assertEqual(self.i2c.closed, 1)


if __name__ == "__main__":
    unittest.main()
