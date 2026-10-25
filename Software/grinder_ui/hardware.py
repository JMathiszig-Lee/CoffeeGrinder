"""Lazy CircuitPython hardware factories for Pico Display Pack 2.8 (PIM715).

Pimoroni schematic: LCD GP16/17/18/19, backlight GP20, Qw/ST SDA GP4 / SCL
GP5. Qw/ST conflicts with the original Pico-CAN-B GP4/GP5 wiring; this module
never opens CAN. See Adafruit's ST7789 Pimoroni Display 2.0 example for the
shared 320x240, rotation=270 initialization.
"""

import struct
import time


def _pin(board, value):
    return getattr(board, value) if isinstance(value, str) else value


def open_display(*, rotation=270, brightness=0.8, auto_refresh=False,
                 clock_pin="GP18", mosi_pin="GP19", dc_pin="GP16",
                 cs_pin="GP17", backlight_pin="GP20"):
    """Return the native 320x240 ST7789 display with a normal root_group API.

    With auto_refresh=False, the renderer calls refresh() when content changes.
    Initialization may wait for panel reset; no firmware replacement is needed.
    """
    if rotation not in (90, 270):
        raise ValueError("Use 90 or 270 degrees for the 320x240 landscape display")
    if not 0 <= brightness <= 1:
        raise ValueError("Display brightness must be between zero and one")
    import board
    import busio
    import displayio
    from fourwire import FourWire
    from adafruit_st7789 import ST7789

    spi = None
    try:
        displayio.release_displays()
        spi = busio.SPI(clock=_pin(board, clock_pin), MOSI=_pin(board, mosi_pin))
        bus = FourWire(spi, command=_pin(board, dc_pin), chip_select=_pin(board, cs_pin),
                       baudrate=24_000_000)
        display = ST7789(bus, width=320, height=240, rotation=rotation,
                         backlight_pin=_pin(board, backlight_pin),
                         auto_refresh=auto_refresh)
        display.brightness = brightness
        return display
    except BaseException:
        displayio.release_displays()
        if spi is not None:
            try:
                spi.deinit()
            except Exception:
                pass
        raise


class Encoder:
    """Cooperative seesaw sampling; read() never sleeps or waits for a lock.

    Seesaw requires 8 ms between selecting and reading a register. Successive
    calls alternate position and button transactions across that interval,
    returning the latest complete values while the next transaction matures.
    Render pauses do not expire a transaction; repeated I2C lock contention
    for 250 ms does. Native I2C errors propagate to the caller.
    """

    def __init__(self, i2c, *, address=0x36, invert=False, clock=time,
                 position=0, pressed=False, chip_id=None, firmware_version=None):
        self.i2c, self.address, self.clock = i2c, address, clock
        self.invert = invert
        self.position, self.pressed = int(position), bool(pressed)
        self.chip_id, self.firmware_version = chip_id, firmware_version
        self._buffer = bytearray(4)
        self._position_pending = True
        self._ready_at = None
        self._busy_since = None
        self._closed = False

    def read(self):
        if self._closed:
            raise RuntimeError("Encoder is closed")
        now = self.clock.monotonic()
        if self._ready_at is not None and now < self._ready_at:
            return self.position, self.pressed
        if not self.i2c.try_lock():
            if self._busy_since is None:
                self._busy_since = now
            elif now - self._busy_since > 0.25:
                raise RuntimeError("I2C bus lock timeout")
            return self.position, self.pressed
        self._busy_since = None
        try:
            if self._ready_at is not None:
                self.i2c.readfrom_into(self.address, self._buffer)
                if self._position_pending:
                    position = struct.unpack(">i", self._buffer)[0]
                    self.position = -position if self.invert else position
                else:
                    self.pressed = not bool(struct.unpack(">I", self._buffer)[0] & (1 << 24))
                self._position_pending = not self._position_pending
            command = b"\x11\x30" if self._position_pending else b"\x01\x04"
            self.i2c.writeto(self.address, command)
            self._ready_at = self.clock.monotonic() + 0.008
        finally:
            self.i2c.unlock()
        return self.position, self.pressed

    def close(self):
        if not self._closed:
            self._closed = True
            self.i2c.deinit()


def open_encoder(*, sda="GP4", scl="GP5", address=0x36, invert=False, clock=time):
    """Probe the Qw/ST encoder and return Encoder; missing hardware raises.

    Startup uses Adafruit's hardware-ID check plus product firmware ID 4991,
    and configures the active-low switch on seesaw pin 24 with its pull-up.
    The encoder's NeoPixel is seesaw pin 6; it is left untouched.
    The default direction was checked on the connected encoder: clockwise
    increases RPM. Other encoder orientations can select invert=True.
    """
    if type(address) is not int or not 0x08 <= address <= 0x77:
        raise ValueError("Invalid encoder I2C address")
    import board
    import busio
    from adafruit_seesaw.seesaw import Seesaw

    i2c = busio.I2C(scl=_pin(board, scl), sda=_pin(board, sda), frequency=100_000)
    try:
        device = Seesaw(i2c, addr=address)
        version = device.get_version()
        if (version >> 16) & 0xFFFF != 4991:
            raise RuntimeError("Unexpected encoder firmware product %d; expected 4991"
                               % ((version >> 16) & 0xFFFF))
        device.pin_mode(24, device.INPUT_PULLUP)
        raw_position = device.encoder_position()
        return Encoder(i2c, address=address, invert=invert, clock=clock,
                       position=-raw_position if invert else raw_position,
                       pressed=not device.digital_read(24), chip_id=device.chip_id,
                       firmware_version=version)
    except BaseException:
        i2c.deinit()
        raise
