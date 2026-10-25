"""Explicitly saved RPM and prototype options; hardware access stays lazy.

Bytes 0..7 retain the legacy CGUI RPM record. Bytes 8..27 hold a versioned
CGOP option record with CRC16; all other NVM bytes belong to other users.
Startup never writes, and unavailable/corrupt options leave automation off.
"""

import math
import struct


DEFAULT_RPM = 110
MIN_RPM = 5
MAX_RPM = 110
_MAGIC = b"CGUI"
_SIZE = 8
_OPTIONS_MAGIC = b"CGOP"
_OPTIONS_VERSION = 1
_OPTIONS_FORMAT = "<BBBBHHBHB"
_OPTIONS_SIZE = 20
_OPTIONS_END = _SIZE + _OPTIONS_SIZE

DEFAULT_OPTIONS = {
    "autostop_enabled": False,
    "autostop_delay_s": 3.0,
    "unload_percent": 25,
    "load_hysteresis": 8,
    "stall_recovery_enabled": False,
    "stall_percent": 80,
    "stall_detect_s": 0.4,
    "reverse_rpm": 20,
    "reverse_duration_s": 0.6,
    "recovery_attempts": 2,
}
OPTION_RANGES = {
    "autostop_delay_s": (0.5, 30.0),
    "unload_percent": (0, 80),
    "load_hysteresis": (1, 30),
    "stall_percent": (50, 100),
    "stall_detect_s": (0.1, 3.0),
    "reverse_rpm": (5, 30),
    "reverse_duration_s": (0.1, 2.0),
    "recovery_attempts": (1, 3),
}


class SettingsError(RuntimeError):
    pass


def validate_rpm(rpm):
    if type(rpm) is not int or not MIN_RPM <= rpm <= MAX_RPM:
        raise ValueError("Default RPM must be an integer from 5 to 110")
    return rpm


def validate_options(values, base=None):
    """Validate a partial update and return a complete independent dictionary.

    Timings are stored at millisecond resolution. Flags require actual bools,
    counts/RPM/percentages actual ints; unknown keys are rejected.
    """
    if not isinstance(values, dict):
        raise ValueError("Settings options must be a dictionary")
    result = dict(DEFAULT_OPTIONS if base is None else base)
    for name, value in values.items():
        if name not in DEFAULT_OPTIONS:
            raise ValueError("Unknown setting: " + str(name))
        result[name] = value
    for name, value in result.items():
        if name.endswith("_enabled"):
            if type(value) is not bool:
                raise ValueError(name + " must be a bool")
        else:
            low, high = OPTION_RANGES[name]
            timing = name.endswith("_s")
            if ((type(value) not in (int, float) if timing else type(value) is not int)
                    or not low <= value <= high or not math.isfinite(value)):
                raise ValueError(name + " must be between %s and %s" % (low, high))
            if timing:
                result[name] = round(value * 1000) / 1000
    if result["unload_percent"] + result["load_hysteresis"] > 100:
        raise ValueError("Unload threshold plus hysteresis must not exceed 100")
    return result


def _blank(record):
    return record in (bytes(len(record)), b"\xff" * len(record))


def _crc16(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def _option_record(values):
    flags = int(values["autostop_enabled"]) | int(values["stall_recovery_enabled"]) << 1
    payload = struct.pack(_OPTIONS_FORMAT, flags, values["unload_percent"],
                          values["load_hysteresis"], values["stall_percent"],
                          round(values["autostop_delay_s"] * 1000),
                          round(values["stall_detect_s"] * 1000), values["reverse_rpm"],
                          round(values["reverse_duration_s"] * 1000), values["recovery_attempts"])
    record = _OPTIONS_MAGIC + bytes((_OPTIONS_VERSION, len(payload))) + payload
    return record + struct.pack("<H", _crc16(record))


def _decode_options(record):
    if record[:6] != _OPTIONS_MAGIC + bytes((_OPTIONS_VERSION, 12)):
        raise ValueError("Unknown settings extension version or length")
    if struct.unpack("<H", record[-2:])[0] != _crc16(record[:-2]):
        raise ValueError("Settings checksum mismatch")
    flags, unload, hysteresis, stall, delay, detect, rpm, duration, attempts = struct.unpack(
        _OPTIONS_FORMAT, record[6:-2])
    if flags & ~3:
        raise ValueError("Unknown settings flags")
    return validate_options({"autostop_enabled": bool(flags & 1),
                             "stall_recovery_enabled": bool(flags & 2),
                             "autostop_delay_s": delay / 1000,
                             "unload_percent": unload, "load_hysteresis": hysteresis,
                             "stall_percent": stall, "stall_detect_s": detect / 1000,
                             "reverse_rpm": rpm, "reverse_duration_s": duration / 1000,
                             "recovery_attempts": attempts})


class MemorySettings:
    """Readable fallback which never claims that a save survived a restart."""

    persistent = False

    def __init__(self, default_rpm=DEFAULT_RPM, message=None, values=None):
        self.default_rpm = validate_rpm(default_rpm)
        self.message = message or "Persistent settings unavailable"
        self._values = validate_options({} if values is None else values)

    @property
    def values(self):
        return dict(self._values)

    def load(self):
        return self.default_rpm

    def save(self, rpm):
        validate_rpm(rpm)
        raise SettingsError(self.message + "; default was not saved")

    def save_options(self, values):
        validate_options(values, self._values)
        raise SettingsError(self.message + "; options were not saved")


class NvmSettings:
    """Preserve legacy RPM, foreign extensions and all bytes after offset 27."""

    persistent = True

    def __init__(self, nvm):
        self.nvm = nvm
        self.default_rpm = DEFAULT_RPM
        self.message = None
        self._options_message = None
        self._values = dict(DEFAULT_OPTIONS)
        self._foreign = False
        self._valid = False
        try:
            if len(nvm) < _SIZE:
                raise ValueError("NVM has fewer than eight bytes")
            record = bytes(nvm[:_SIZE])
        except Exception as error:
            self.message = "Settings unavailable: " + str(error)
            self.persistent = False
            return
        if not _blank(record) and record[:4] != _MAGIC:
            self._foreign = True
            self.message = "NVM belongs to another application; save unavailable"
            return
        if not _blank(record):
            rpm, check = struct.unpack("<HH", record[4:])
            if MIN_RPM <= rpm <= MAX_RPM and check == rpm ^ 0xFFFF:
                self.default_rpm = rpm
                self._valid = True
            else:
                self.message = "Saved default is corrupt; using 110 RPM; automation disabled"
                return
        if len(nvm) >= _OPTIONS_END:
            try:
                extension = bytes(nvm[_SIZE:_OPTIONS_END])
                if not _blank(extension):
                    if extension[:6] != _OPTIONS_MAGIC + bytes((_OPTIONS_VERSION, 12)):
                        self._options_message = "Foreign settings extension preserved; automation disabled"
                    else:
                        self._values = _decode_options(extension)
            except Exception as error:
                self._options_message = "Saved options are corrupt; automation disabled: " + str(error)
            self.message = self._options_message

    @property
    def values(self):
        return dict(self._values)

    def load(self):
        return self.default_rpm

    def save(self, rpm):
        validate_rpm(rpm)
        if not self.persistent:
            raise SettingsError((self.message or "Settings unavailable") + "; default was not saved")
        if self._foreign:
            raise SettingsError("Foreign NVM record preserved; default was not saved")
        record = _MAGIC + struct.pack("<HH", rpm, rpm ^ 0xFFFF)
        try:
            current = bytes(self.nvm[:_SIZE])
            if current[:4] != _MAGIC and not _blank(current):
                self._foreign = True
                raise SettingsError("Foreign NVM record preserved; default was not saved")
            if current == record:
                self.default_rpm = rpm
                self._valid = True
                self.message = self._options_message
                return False
            self.nvm[:_SIZE] = record
            if bytes(self.nvm[:_SIZE]) != record:
                raise ValueError("NVM readback mismatch")
        except Exception as error:
            self.message = "Default was not saved: " + str(error)
            raise SettingsError(self.message)
        self.default_rpm = rpm
        self._valid = True
        self.message = self._options_message
        return True

    def save_options(self, values):
        updated = validate_options(values, self._values)
        if not self.persistent or self._foreign:
            raise SettingsError("Settings storage unavailable or foreign; options were not saved")
        record = _option_record(updated)
        try:
            if len(self.nvm) < _OPTIONS_END:
                raise ValueError("NVM needs at least 28 bytes for options")
            rpm_record = bytes(self.nvm[:_SIZE])
            if rpm_record[:4] != _MAGIC and not _blank(rpm_record):
                self._foreign = True
                raise ValueError("Foreign NVM record preserved")
            if rpm_record[:4] == _MAGIC:
                rpm, check = struct.unpack("<HH", rpm_record[4:])
                if not MIN_RPM <= rpm <= MAX_RPM or check != rpm ^ 0xFFFF:
                    raise ValueError("Repair the corrupt RPM record before saving options")
            current = bytes(self.nvm[_SIZE:_OPTIONS_END])
            if not _blank(current) and current[:6] != record[:6]:
                raise ValueError("Foreign settings extension preserved")
            changed = current != record
            if changed:
                self.nvm[_SIZE:_OPTIONS_END] = record
            if bytes(self.nvm[_SIZE:_OPTIONS_END]) != record:
                raise ValueError("NVM options readback mismatch")
        except Exception as error:
            self.message = "Options were not saved: " + str(error)
            raise SettingsError(self.message)
        self._values = updated
        self._options_message = None
        self.message = None
        return changed


NVMSettings = NvmSettings


def open_settings():
    """Read CircuitPython NVM when requested; never write during startup."""
    try:
        import microcontroller
        store = NvmSettings(microcontroller.nvm)
        if not store.persistent:
            return MemorySettings(message=store.message)
        if isinstance(store.nvm, bytes) or getattr(store.nvm, "readonly", False):
            return MemorySettings(store.default_rpm, "Persistent settings are read-only", store.values)
        return store
    except Exception as error:
        return MemorySettings(message="Persistent settings unavailable: " + str(error))
