"""Pure CAN Simple codecs for the verified SteadyWin firmware 0.5.14 table.

Reference: manufacturer endpoints_0.5.14.json and GIM6010-8 rev2.2 manual.
No operation in this module accesses hardware or sends a frame.
"""

import math
import struct

FIRMWARE_VERSION = (0, 5, 14)
GEAR_RATIO = 8.0
AXIS_IDLE = 1
AXIS_CLOSED_LOOP = 8
WATCHDOG_ERROR = 0x800
HEARTBEAT = 0x01
ESTOP = 0x02
RX_SDO = 0x04
TX_SDO = 0x05
SET_AXIS_STATE = 0x07
ENCODER = 0x09
SET_CONTROLLER_MODE = 0x0B
SET_VELOCITY = 0x0D
SET_LIMITS = 0x0F
IQ = 0x14
BUS_VI = 0x17
CLEAR_ERRORS = 0x18
TELEMETRY_COMMANDS = (HEARTBEAT, RX_SDO, ENCODER, IQ, BUS_VI)

FIELDS = {
    "voltage": (2, "f"), "hw_major": (6, "B"), "hw_minor": (7, "B"),
    "hw_variant": (8, "B"), "fw_major": (9, "B"), "fw_minor": (10, "B"),
    "fw_revision": (11, "B"), "unreleased": (12, "B"),
    "step_dir": (147, "?"), "sensorless": (149, "?"),
    "watchdog_timeout": (150, "f"), "watchdog_enabled": (151, "?"),
    "heartbeat_rate_ms": (179, "I"), "encoder_rate_ms": (180, "I"),
    "iq_rate_ms": (186, "I"), "bus_vi_rate_ms": (188, "I"),
    "calibrated": (192, "?"), "current_limit": (246, "f"),
    "input_velocity": (267, "f"), "input_torque": (268, "f"),
    "enable_vel_limit": (279, "?"), "enable_overspeed_error": (282, "?"),
    "control_mode": (283, "B"), "input_mode": (284, "B"),
    "velocity_limit": (289, "f"), "ready": (330, "?"),
}
READ_ONLY_FIELDS = ("voltage", "hw_major", "hw_minor", "hw_variant",
                    "fw_major", "fw_minor", "fw_revision", "unreleased", "calibrated", "ready")


def command_id(node_id, command):
    if type(node_id) is not int or not 1 <= node_id <= 63:
        raise ValueError("node_id must be an integer from 1 to 63")
    if type(command) is not int or not 0 <= command <= 31:
        raise ValueError("CAN Simple command must be an integer from 0 to 31")
    return (node_id << 5) | command


def _field(name):
    if name not in FIELDS:
        raise ValueError("Unknown firmware 0.5.14 field: " + str(name))
    return FIELDS[name]


def _payload(data):
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != 8:
        raise ValueError("Expected an eight-byte data frame")
    return bytes(data)


def _finite(value):
    if isinstance(value, bool):
        raise ValueError("Expected a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Expected a finite number")
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def read_request(name):
    endpoint, _ = _field(name)
    return struct.pack("<BHBI", 0, endpoint, 0, 0)


def write_request(name, value):
    endpoint, codec = _field(name)
    if name in READ_ONLY_FIELDS:
        raise ValueError("Field is read-only: " + name)
    if codec == "?":
        if type(value) is not bool:
            raise ValueError("Boolean fields require True or False")
        raw = bytes((int(value),))
    elif codec == "f":
        try:
            raw = struct.pack("<f", _finite(value))
        except (OverflowError, ValueError):
            raise ValueError("Float value cannot be represented")
        if not math.isfinite(struct.unpack("<f", raw)[0]):
            raise ValueError("Float value cannot be represented")
    else:
        maximum = 255 if codec == "B" else 0xFFFFFFFF
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("Unsigned integer field is out of range")
        raw = struct.pack("<" + codec, value)
    return struct.pack("<BHB", 1, endpoint, 0) + raw + bytes(4 - len(raw))


def read_reply(name, data):
    endpoint, codec = _field(name)
    data = _payload(data)
    if data[:4] != struct.pack("<BHB", 0, endpoint, 0):
        raise ValueError("SDO reply opcode, endpoint or reserved byte does not match")
    wire_codec = "B" if codec == "?" else codec
    size = struct.calcsize("<" + wire_codec)
    value = struct.unpack("<" + wire_codec, data[4:4 + size])[0]
    if codec == "?":
        if value not in (0, 1):
            raise ValueError("Malformed boolean SDO reply")
        return bool(value)
    if codec == "f" and not math.isfinite(value):
        raise ValueError("Nonfinite float SDO reply")
    return value


def decode_telemetry(command, data):
    data = _payload(data)
    if command == HEARTBEAT:
        return struct.unpack("<I", data[:4])[0], data[4], data[5]
    if command not in (ENCODER, IQ, BUS_VI):
        raise ValueError("Unsupported telemetry command")
    values = struct.unpack("<ff", data)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Nonfinite telemetry value")
    return values


def output_rpm(rotor_rev_s, gear_ratio=GEAR_RATIO):
    ratio = _finite(gear_ratio)
    if ratio <= 0:
        raise ValueError("gear_ratio must be positive")
    return _finite(rotor_rev_s) * 60.0 / ratio


def rotor_velocity(rpm, gear_ratio=GEAR_RATIO):
    ratio = _finite(gear_ratio)
    if ratio <= 0:
        raise ValueError("gear_ratio must be positive")
    return _finite(rpm) * ratio / 60.0
