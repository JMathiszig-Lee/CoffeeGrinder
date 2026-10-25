"""Pico-CAN-B transport with one-shot, single-frame CAN transmission.

Hardware imports occur only in open_transport(). Opening the bus sends no
motor commands. The application must call update() frequently (e.g. 5 ms).
Normal update/send/receive calls do not wait or sleep. Explicit abort/close
and exceptional TX cleanup may poll SPI for at most 25 ms per operation.
"""

import time

from .protocol import TELEMETRY_COMMANDS, command_id

_CANSTAT = 0x0E
_CANCTRL = 0x0F
_CANINTF = 0x2C
_EFLG = 0x2D
_TX_CONTROLS = (0x30, 0x40, 0x50)
_TIMING = (0x86, 0xF0, 0x00)  # CNF3, CNF2, CNF1: 16 MHz, 500 kbit/s.
_DIAGNOSTIC_INTERVAL = 0.020


class _SPIBus:
    """Keep Adafruit's default 100 kHz SPIDevice at the verified 1 MHz rate."""

    def __init__(self, spi):
        self.spi = spi

    def configure(self, *, baudrate=1_000_000, polarity=0, phase=0, **options):
        self.spi.configure(baudrate=1_000_000, polarity=polarity, phase=phase, **options)

    def __getattr__(self, name):
        return getattr(self.spi, name)


class TransportError(RuntimeError):
    """A controller, SPI or CAN transmission check failed."""


class Registers:
    """Direct diagnostic SPI operations; each operation tries the lock once."""

    def __init__(self, spi, cs, clock=time):
        self.spi, self.cs, self.clock = spi, cs, clock

    def transfer(self, command, length=0):
        if not self.spi.try_lock():
            raise TransportError("SPI bus is busy")
        try:
            self.spi.configure(baudrate=1_000_000, polarity=0, phase=0)
            self.cs.value = False
            self.spi.write(command)
            result = bytearray(length)
            if length:
                self.spi.readinto(result)
            return result
        finally:
            self.cs.value = True
            self.spi.unlock()

    def read(self, address, length=1):
        return self.transfer(bytes((0x03, address)), length)

    def modify(self, address, mask, value):
        self.transfer(bytes((0x05, address, mask, value)))

    def probe(self):
        self.transfer(b"\xC0")
        self.clock.sleep(0.01)  # Startup only; RESET enters configuration mode.
        if tuple(self.read(_CANSTAT, 2)) != (0x80, 0x87):
            raise TransportError("MCP2515 reset signature failed; check SPI wiring and power")

    def verify(self, mode, one_shot=False):
        status, control = self.read(_CANSTAT, 2)
        if status & 0xE0 != mode or control & 0xE0 != mode:
            raise TransportError("MCP2515 operating mode verification failed")
        if tuple(self.read(0x28, 3)) != _TIMING:
            raise TransportError("MCP2515 500 kbit/s / 16 MHz timing verification failed")
        if one_shot and not control & 0x08:
            raise TransportError("MCP2515 one-shot mode verification failed")

    def set_mode(self, mode):
        self.modify(_CANCTRL, 0xE0, mode)
        deadline = self.clock.monotonic() + 0.025
        while self.read(_CANSTAT)[0] & 0xE0 != mode:
            if self.clock.monotonic() >= deadline:
                raise TransportError("MCP2515 operating mode change timed out")


class Transport:
    """Dependency-injectable CAN transport; no motor state or protocol writes."""

    def __init__(self, can, message_type, registers, node_id=1, clock=time,
                 tx_timeout=0.025, resources=()):
        command_id(node_id, 0)
        if not 0 < tx_timeout <= 0.1:
            raise ValueError("tx_timeout must be positive and at most 0.1 seconds")
        self.can, self.message_type, self.registers = can, message_type, registers
        self.node_id, self.clock, self.tx_timeout = node_id, clock, tx_timeout
        self.resources = resources
        self._pending = False
        self._deadline = None
        self._closed = False
        self._rx = []
        self._next_diagnostics = 0.0
        self.rx_overflows = self.tx_failures = self.rx_frames = self.rx_filtered = 0
        self.error_flags = 0

    @property
    def pending(self):
        return self._pending

    @property
    def diagnostics(self):
        return {"rx_overflows": self.rx_overflows, "error_flags": self.error_flags,
                "tx_failures": self.tx_failures, "rx_frames": self.rx_frames,
                "rx_filtered": self.rx_filtered, "pending": self.pending}

    def _check_open(self):
        if self._closed:
            raise TransportError("CAN transport is closed")

    def _controls(self):
        return [self.registers.read(address)[0] for address in _TX_CONTROLS]

    def abort(self):
        self._check_open()
        self._pending = False
        self._deadline = None
        self.registers.modify(_CANCTRL, 0x10, 0x10)
        deadline = self.clock.monotonic() + 0.025
        while any(value & 0x08 for value in self._controls()):
            if self.clock.monotonic() >= deadline:
                # Leave ABAT asserted when the controller cannot confirm abort.
                raise TransportError("CAN transmit abort timed out")
        self.registers.modify(_CANINTF, 0x1C, 0)
        self.registers.modify(_CANCTRL, 0x10, 0)

    def _tx_failure(self, reason):
        self.tx_failures += 1
        try:
            self.abort()
        except Exception as error:
            raise TransportError(reason + "; abort failed: " + str(error))
        raise TransportError(reason)

    def _update_tx(self, force=False):
        """Check controller/TX state without consuming any received frames."""
        now = self.clock.monotonic()
        diagnostics_due = force or now >= self._next_diagnostics
        if diagnostics_due:
            self.error_flags = self.registers.read(_EFLG)[0]
            overflow = self.error_flags & 0xC0
            if overflow:
                self.rx_overflows += bool(overflow & 0x40) + bool(overflow & 0x80)
                self.registers.modify(_EFLG, 0xC0, 0)
            if self.error_flags & 0x20:
                self._tx_failure("CAN controller entered bus-off")
            status, control = self.registers.read(_CANSTAT, 2)
            if status & 0xE0 or control & 0xE0 or not control & 0x08 or control & 0x10:
                self._tx_failure("CAN normal/one-shot mode was lost")
            self._next_diagnostics = now + _DIAGNOSTIC_INTERVAL
        if not self._pending and not diagnostics_due:
            return
        controls = self._controls()
        # ABTF/MLOA/TXERR are read-only historical flags. Hardware clears them
        # when the next TXREQ starts; an abort must not poison a future send.
        if self._pending and any(value & 0x70 for value in controls):
            self._tx_failure("CAN transmission failed (abort, arbitration loss or TX error)")
        queued = sum(bool(value & 0x08) for value in controls)
        if queued > 1 or (queued and not self._pending):
            self._tx_failure("Unexpected queued CAN transmission")
        if self._pending:
            if not queued:
                self._pending, self._deadline = False, None
            elif self.clock.monotonic() >= self._deadline:
                self._tx_failure("CAN transmission deadline expired")

    def _drain_rx(self, maximum):
        consumed = 0
        for _ in range(maximum):
            frame = self.can.read_message()
            if frame is None:
                break
            consumed += 1
            if (frame.extended or not hasattr(frame, "data") or
                    frame.id >> 5 != self.node_id or frame.id & 0x1F not in TELEMETRY_COMMANDS):
                self.rx_filtered += 1
                continue
            if len(self._rx) == 64:
                self._rx.pop(0)
                self.rx_overflows += 1
            self._rx.append((frame.id, bytes(frame.data)))
            self.rx_frames += 1
        return consumed

    def update(self):
        """Drain RX around status checks; at most 32 frames, no normal waits."""
        self._check_open()
        consumed = self._drain_rx(32)
        self._update_tx()
        self._drain_rx(32 - consumed)

    def receive(self):
        self._check_open()
        return self._rx.pop(0) if self._rx else None

    def send(self, arbitration_id, data):
        self._check_open()
        if (type(arbitration_id) is not int or not 0 <= arbitration_id <= 0x7FF
                or arbitration_id >> 5 != self.node_id):
            raise ValueError("Expected a standard CAN ID for the configured node")
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) > 8:
            raise ValueError("CAN data must contain zero to eight bytes")
        self._update_tx(force=True)
        if self._pending:
            return False
        try:
            queued = self.can.send(self.message_type(arbitration_id, bytes(data), extended=False))
            if queued is False:
                self._tx_failure("CAN frame was not queued")
        except TransportError:
            raise
        except Exception as error:
            self._tx_failure("CAN send failed: " + str(error))
        self._pending = True
        self._deadline = self.clock.monotonic() + self.tx_timeout
        return True

    def close(self):
        if self._closed:
            return
        failure = None
        try:
            self.abort()
        except Exception as error:
            failure = error
        try:
            self.registers.set_mode(0x60)
            self.registers.verify(0x60)
        except Exception as error:
            failure = error
        finally:
            self._closed = True
            self._rx = []
            for resource in self.resources:
                resource.deinit()
        if failure is not None:
            raise TransportError("CAN close failed: " + str(failure))


def open_transport(node_id=1, clock=time):
    """Open the fixed Pico-CAN-B wiring; initialization sends no CAN frames."""
    command_id(node_id, 0)
    import board
    import busio
    import digitalio
    from adafruit_mcp2515 import MCP2515
    from adafruit_mcp2515.canio import Message

    cs = spi = registers = None
    try:
        cs = digitalio.DigitalInOut(board.GP5)
        cs.switch_to_output(value=True)
        spi = busio.SPI(clock=board.GP6, MOSI=board.GP7, MISO=board.GP4)
        registers = Registers(spi, cs, clock)
        registers.probe()
        driver_spi = _SPIBus(spi)
        MCP2515(driver_spi, cs, silent=True, loopback=False,
                baudrate=500_000, crystal_freq=16_000_000)
        registers.verify(0x60)
        can = MCP2515(driver_spi, cs, silent=False, loopback=False,
                      baudrate=500_000, crystal_freq=16_000_000)
        registers.modify(_CANCTRL, 0x08, 0x08)
        registers.verify(0x00, one_shot=True)
        # Adafruit's public filter API places identical exact masks in one
        # two-filter bank. Five telemetry/SDO IDs therefore use bounded software
        # filtering, avoiding private driver state and unreliable partial masks.
        return Transport(can, Message, registers, node_id, clock, resources=(spi, cs))
    except BaseException:
        if registers is not None:
            try:
                registers.set_mode(0x60)
            except Exception:
                pass
        for resource in (spi, cs):
            if resource is not None:
                resource.deinit()
        raise
