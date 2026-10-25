"""CircuitPython: passive Waveshare Pico-CAN-B SPI and CAN commissioning.

Copy this file to CIRCUITPY/code.py. Requires adafruit_mcp2515 and
adafruit_bus_device matching the installed CircuitPython version.
Only a fixed 500 kbit/s, 15 second capture is performed; reset to repeat.
"""

import time

CAN_BITRATE = 500_000
CRYSTAL_HZ = 16_000_000
CAPTURE_SECONDS = 15.0
MAX_FRAME_LINES = 50
FRAME_LOG_INTERVAL = 0.1
MODE_MASK = 0xE0
LISTEN_ONLY = 0x60
CANSTAT = 0x0E
CANCTRL = 0x0F
CNF3 = 0x28
EFLG = 0x2D
# Adafruit's 16 MHz / 500 kbit/s table, ordered CNF1, CNF2, CNF3.
EXPECTED_TIMING = (0x00, 0xF0, 0x86)


class Registers:
    """Small direct SPI diagnostic path, independent of the CAN library."""

    def __init__(self, spi, cs, clock=time):
        self.spi = spi
        self.cs = cs
        self.clock = clock

    def transfer(self, command, length=0):
        deadline = self.clock.monotonic() + 0.25
        while not self.spi.try_lock():
            if self.clock.monotonic() >= deadline:
                raise RuntimeError("SPI lock timeout")
            self.clock.sleep(0.001)
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

    def probe(self, emit=print):
        before = self.read(CANSTAT, 2)
        emit("SPI before reset: CANSTAT=0x%02X CANCTRL=0x%02X" % tuple(before))
        # Reset enters configuration mode and clears pending TX requests.
        self.transfer(b"\xC0")
        self.clock.sleep(0.01)
        status, control = self.read(CANSTAT, 2)
        emit("SPI reset check: CANSTAT=0x%02X CANCTRL=0x%02X" % (status, control))
        # Stuck-low/stuck-high MISO must fail before library initialization.
        if status != 0x80 or control != 0x87:
            raise RuntimeError("MCP2515 reset signature failed; check power, CS and SPI wiring")

    def verify(self, emit=None):
        status, control = self.read(CANSTAT, 2)
        timing = tuple(reversed(self.read(CNF3, 3)))
        if emit is not None:
            emit("CANSTAT=0x%02X CANCTRL=0x%02X; CNF1/2/3=%02X/%02X/%02X"
                 % ((status, control) + timing))
        if status & MODE_MASK != LISTEN_ONLY or control & MODE_MASK != LISTEN_ONLY:
            raise RuntimeError("Listen-only mode verification failed; capture refused")
        if timing != EXPECTED_TIMING:
            raise RuntimeError("500 kbit/s / 16 MHz timing verification failed; capture refused")

    def preserve_listen_only(self, emit=print):
        # No retry into normal mode, and no CAN-library cleanup that changes mode.
        try:
            self.transfer(bytes((0x05, CANCTRL, MODE_MASK, LISTEN_ONLY)))
            self.clock.sleep(0.01)
            status = self.read(CANSTAT)[0]
            if status & MODE_MASK != LISTEN_ONLY:
                raise RuntimeError("CANSTAT=0x%02X" % status)
            emit("SAFE IDLE: MCP2515 remains in listen-only mode")
        except Exception as error:
            emit("SAFE IDLE: could not verify listen-only: %s" % error)


def capture(can, registers, clock=time, emit=print, seconds=CAPTURE_SECONDS):
    """Drain without unbounded storage or per-frame console output."""
    counts = {"total": 0, "standard": 0, "extended": 0, "rtr": 0,
              "logged": 0, "overflow_flags": 0}
    started = clock.monotonic()
    deadline = started + seconds
    next_log = started
    next_check = started
    while clock.monotonic() < deadline:
        now = clock.monotonic()
        if now >= next_check:
            registers.verify()
            counts["overflow_flags"] |= registers.read(EFLG)[0] & 0xC0
            next_check = now + 1.0
        # Nonblocking library API; unlike a receive-until-empty loop this stays
        # bounded even on a continuously busy bus.
        frame = can.read_message()
        if frame is None:
            clock.sleep(0.001)
            continue
        counts["total"] += 1
        counts["extended" if frame.extended else "standard"] += 1
        # Adafruit RTR objects expose length; data frames expose data.
        is_rtr = not hasattr(frame, "data")
        if is_rtr:
            counts["rtr"] += 1
        if counts["logged"] < MAX_FRAME_LINES and now >= next_log:
            kind = "EXT" if frame.extended else "STD"
            if is_rtr:
                detail = "RTR DLC=%d" % frame.length
            else:
                detail = "DLC=%d DATA=%s" % (
                    len(frame.data), " ".join("%02X" % value for value in frame.data))
            emit("%7.3fs %s ID=0x%X %s" % (now - started, kind, frame.id, detail))
            counts["logged"] += 1
            next_log = now + FRAME_LOG_INTERVAL
    registers.verify()
    counts["overflow_flags"] |= registers.read(EFLG)[0] & 0xC0
    emit("CAPTURE END: total=%d standard=%d extended=%d RTR=%d logged=%d suppressed=%d"
         % (counts["total"], counts["standard"], counts["extended"], counts["rtr"],
            counts["logged"], counts["total"] - counts["logged"]))
    emit("RX overflow flags=0x%02X (capture may omit frames if nonzero)"
         % counts["overflow_flags"])
    if counts["total"] == 0:
        emit("No frames observed at 500 kbit/s. A silent drive may need polling or a different bitrate.")
    return counts


def run_session(spi, cs, can_factory, clock=time, emit=print):
    registers = Registers(spi, cs, clock)
    try:
        registers.probe(emit)
        can = can_factory(spi, cs, silent=True, loopback=False,
                          baudrate=CAN_BITRATE, crystal_freq=CRYSTAL_HZ)
        registers.verify(emit)
        emit("PASSIVE CAPTURE: 15 seconds at 500 kbit/s; no ACK, requests or motor commands")
        return capture(can, registers, clock, emit)
    finally:
        registers.preserve_listen_only(emit)


def main():
    import board
    import busio
    import digitalio
    from adafruit_mcp2515 import MCP2515

    print("Pico 2 + Waveshare Pico-CAN-B passive bring-up")
    print("SPI: SCK=GP6 MOSI=GP7 MISO=GP4 CS=GP5; MCP2515 crystal=16 MHz")
    cs = digitalio.DigitalInOut(board.GP5)
    cs.switch_to_output(value=True)
    spi = busio.SPI(clock=board.GP6, MOSI=board.GP7, MISO=board.GP4)
    try:
        run_session(spi, cs, MCP2515)
    except Exception as error:
        print("STOP: %s: %s" % (type(error).__name__, error))
    print("Capture stopped. Reset the Pico to repeat; no automatic retries.")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
