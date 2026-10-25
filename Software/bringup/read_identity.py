"""One-shot, read-only CAN Simple node 1 identity query; never commands motion.

Manufacturer SDK replies to RxSdo reads on the same command ID, 0x004.
Endpoints 6..12 are read-only uint8 properties in the vendor endpoint tables.
Run via the serial REPL; leave the persistent /code.py passive program intact.

Sources: GIM6010-8 Motor Manual rev2.2, printed pp45-46;
https://gitee.com/cyberbeast/mwmotorsdk/blob/master/MWSDK/MWMotor.c
https://bl.cyberbeast.cn/actuator/endpoints_0.5.14.json
"""

import struct
import time

VERSION_FIELDS = ((6, "hw_version_major"), (7, "hw_version_minor"),
                  (8, "hw_version_variant"), (9, "fw_version_major"),
                  (10, "fw_version_minor"), (11, "fw_version_revision"),
                  (12, "fw_version_unreleased"))
HEARTBEAT_ID = 0x21
READ_ID = 0x24
ENCODER_ID = 0x29
SESSION_SECONDS = 5.0
PHASE_SECONDS = 0.4


def run_identity(spi, cs, can_factory, registers, message_type, clock=time, emit=print):
    deadline = clock.monotonic() + SESSION_SECONDS
    values = {}
    raw_lines = 0
    latest_encoder = None

    def verify_mode(mode):
        status, control = registers.read(0x0E, 2)
        if status & 0xE0 != mode or control & 0xE0 != mode:
            raise RuntimeError("Unexpected MCP2515 mode; query stopped")
        if tuple(registers.read(0x28, 3)) != (0x86, 0xF0, 0x00):
            raise RuntimeError("Unexpected CAN timing; query stopped")

    def observe(frame):
        nonlocal raw_lines, latest_encoder
        if frame is None or frame.extended or not hasattr(frame, "data"):
            return None
        data = bytes(frame.data)
        if raw_lines < 12:
            emit("RX ID=0x%03X DLC=%d DATA=%s" % (
                frame.id, len(data), " ".join("%02X" % value for value in data)))
            raw_lines += 1
        if len(data) != 8:
            return None
        if frame.id == HEARTBEAT_ID:
            if int.from_bytes(data[:4], "little") or data[4] != 1 or data[5] & 0x0F:
                raise RuntimeError("Node 1 heartbeat is not IDLE with zero errors; query stopped")
        elif frame.id == ENCODER_ID:
            latest_encoder = struct.unpack("<ff", data)
        return frame.id, data

    def await_frame(can, endpoint=None):
        phase_deadline = min(deadline, clock.monotonic() + PHASE_SECONDS)
        while clock.monotonic() < phase_deadline:
            observed = observe(can.read_message())
            if observed is not None:
                frame_id, data = observed
                if endpoint is None and frame_id == HEARTBEAT_ID:
                    return data
                if endpoint is not None and frame_id == READ_ID:
                    if data[:4] == bytes((0, endpoint, 0, 0)):
                        return data
            clock.sleep(0.001)
        raise RuntimeError("Timed out waiting for %s; no retry" % (
            "safe heartbeat" if endpoint is None else "endpoint %d reply" % endpoint))

    try:
        emit("IDENTITY READ: node 1, 500 kbit/s, seven immutable uint8 version properties")
        registers.probe(emit)
        can = can_factory(spi, cs, silent=True, loopback=False,
                          baudrate=500_000, crystal_freq=16_000_000)
        registers.verify(emit)
        await_frame(can)
        emit("Safe IDLE heartbeat received; enabling normal CAN only for read requests")
        can = can_factory(spi, cs, silent=False, loopback=False,
                          baudrate=500_000, crystal_freq=16_000_000)
        verify_mode(0x00)
        for endpoint, field in VERSION_FIELDS:
            # A new, validated heartbeat is required before every read request.
            await_frame(can)
            verify_mode(0x00)
            if clock.monotonic() >= deadline:
                raise RuntimeError("Identity session deadline expired")
            payload = bytes((0, endpoint, 0, 0, 0, 0, 0, 0))
            can.send(message_type(READ_ID, payload, extended=False))
            response = await_frame(can, endpoint)
            values[field] = response[4]
            emit("%s=%d (endpoint=%d, raw value=%s)" % (
                field, response[4], endpoint,
                " ".join("%02X" % value for value in response[4:])))
        emit("Hardware %d.%d variant %d; firmware %d.%d.%d unreleased=%d" % tuple(
            values[field] for _, field in VERSION_FIELDS))
        return values
    finally:
        registers.preserve_listen_only(emit)
        if latest_encoder is not None:
            emit("Latest encoder: position=%.6f rev velocity=%.6f rev/s" % latest_encoder)


def main():
    import board
    import busio
    import digitalio
    from adafruit_mcp2515 import MCP2515
    from adafruit_mcp2515.canio import Message

    # Explicit namespace prevents startup execution and host builtin 'code'
    # module ambiguity. /code.py is the reviewed passive bring-up program.
    passive = {"__name__": "passive_registers"}
    with open("/code.py") as source:
        exec(source.read(), passive)
    cs = digitalio.DigitalInOut(board.GP5)
    cs.switch_to_output(value=True)
    spi = busio.SPI(clock=board.GP6, MOSI=board.GP7, MISO=board.GP4)
    registers = passive["Registers"](spi, cs)
    try:
        run_identity(spi, cs, MCP2515, registers, Message)
    except Exception as error:
        print("IDENTITY STOP: %s: %s" % (type(error).__name__, error))
    finally:
        spi.deinit()
        cs.deinit()


if __name__ == "__main__":
    main()
