"""Persistent option behavior, including legacy and foreign NVM ownership."""

from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui.settings import (DEFAULT_OPTIONS, MemorySettings, NvmSettings,
                                 SettingsError, open_settings, validate_options)


def rpm_record(rpm=104):
    return b"CGUI" + struct.pack("<HH", rpm, rpm ^ 0xFFFF)


class Nvm:
    def __init__(self, data=None):
        self.data = bytearray(rpm_record() + bytes(20) + b"unrelated owner" if data is None else data)
        self.writes = []
        self.fail = self.drop_write = False

    def __len__(self):
        return len(self.data)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        if self.fail:
            raise OSError("Read-only NVM")
        self.writes.append((key, bytes(value)))
        if not self.drop_write:
            self.data[key] = value


class OptionSettingsTests(unittest.TestCase):
    def test_saved_104_rpm_is_preserved_without_startup_writes(self):
        for blank in (bytes(20), b"\xff" * 20):
            nvm = Nvm(rpm_record() + blank + b"keep")
            store = NvmSettings(nvm)
            self.assertEqual(store.load(), 104)
            self.assertEqual(store.values, DEFAULT_OPTIONS)
            self.assertIsNone(store.message)
            self.assertEqual(nvm.writes, [])

    def test_options_roundtrip_partial_updates_and_unchanged_save(self):
        nvm = Nvm()
        before = bytes(nvm.data)
        store = NvmSettings(nvm)
        changes = {"autostop_enabled": True, "autostop_delay_s": 12.345,
                   "unload_percent": 31, "load_hysteresis": 9,
                   "stall_recovery_enabled": True, "stall_percent": 91,
                   "stall_detect_s": 0.7, "reverse_rpm": 25,
                   "reverse_duration_s": 1.2, "recovery_attempts": 3}
        self.assertTrue(store.save_options(changes))
        self.assertEqual(bytes(nvm.data[:8]), before[:8])
        self.assertEqual(bytes(nvm.data[28:]), before[28:])
        self.assertEqual(bytes(nvm.data[8:14]), b"CGOP\x01\x0c")
        self.assertEqual(len(nvm.writes[0][1]), 20)
        restored = NvmSettings(nvm)
        self.assertEqual(restored.values, changes)
        self.assertEqual(restored.default_rpm, 104)
        self.assertFalse(restored.save_options(changes))
        self.assertEqual(len(nvm.writes), 1)
        self.assertTrue(restored.save_options({"reverse_rpm": 20}))
        self.assertEqual(restored.values["reverse_rpm"], 20)
        self.assertTrue(restored.values["autostop_enabled"])
        copy = restored.values
        copy["stall_recovery_enabled"] = False
        self.assertTrue(restored.values["stall_recovery_enabled"])

    def test_rpm_save_does_not_touch_options_or_other_bytes(self):
        nvm = Nvm()
        store = NvmSettings(nvm)
        store.save_options({"autostop_enabled": True})
        extension = bytes(nvm.data[8:])
        self.assertTrue(store.save(75))
        self.assertEqual(bytes(nvm.data[8:]), extension)
        restored = NvmSettings(nvm)
        self.assertEqual(restored.default_rpm, 75)
        self.assertTrue(restored.values["autostop_enabled"])

    def test_corruption_disables_automation_and_is_visible_until_explicit_repair(self):
        nvm = Nvm()
        NvmSettings(nvm).save_options({"autostop_enabled": True,
                                       "stall_recovery_enabled": True})
        nvm.data[18] ^= 1
        snapshot = bytes(nvm.data)
        store = NvmSettings(nvm)
        self.assertEqual(store.values, DEFAULT_OPTIONS)
        self.assertEqual(store.default_rpm, 104)
        self.assertIn("corrupt", store.message)
        self.assertEqual(bytes(nvm.data), snapshot)
        store.save(104)
        self.assertIn("corrupt", store.message)
        self.assertTrue(store.save_options({}))
        self.assertIsNone(store.message)
        self.assertEqual(NvmSettings(nvm).values, DEFAULT_OPTIONS)

    def test_foreign_extension_is_preserved_and_does_not_block_legacy_rpm_save(self):
        for extension in (b"other application!!!", b"CGOP\x02\x0c" + bytes(14)):
            nvm = Nvm(rpm_record() + extension + b"keep")
            before = bytes(nvm.data[8:])
            store = NvmSettings(nvm)
            self.assertEqual(store.values, DEFAULT_OPTIONS)
            with self.assertRaisesRegex(SettingsError, "Foreign"):
                store.save_options({"autostop_enabled": True})
            self.assertEqual(nvm.writes, [])
            self.assertEqual(bytes(nvm.data[8:]), before)
            store.save(90)
            self.assertEqual(bytes(nvm.data[8:]), before)

    def test_foreign_record_and_new_foreign_extension_are_not_overwritten(self):
        nvm = Nvm(b"FOREIGN!" + bytes(20))
        store = NvmSettings(nvm)
        with self.assertRaises(SettingsError):
            store.save_options({})
        self.assertEqual(nvm.writes, [])
        nvm = Nvm()
        store = NvmSettings(nvm)
        nvm.data[8:28] = b"a new owner's record"
        with self.assertRaisesRegex(SettingsError, "Foreign"):
            store.save_options({})
        self.assertEqual(nvm.writes, [])

    def test_failed_or_unverified_saves_leave_current_values_unchanged(self):
        for failure in ("fail", "drop_write"):
            nvm = Nvm()
            store = NvmSettings(nvm)
            setattr(nvm, failure, True)
            with self.assertRaisesRegex(SettingsError, "not saved"):
                store.save_options({"autostop_enabled": True})
            self.assertEqual(store.values, DEFAULT_OPTIONS)
            self.assertEqual(store.default_rpm, 104)

    def test_invalid_types_nonfinite_unknown_and_out_of_range_never_write(self):
        bad_values = {"autostop_enabled": (0, "yes", None),
                      "stall_recovery_enabled": (1,),
                      "autostop_delay_s": (True, float("nan"), float("inf"), 10 ** 1000, 0.49, 30.01),
                      "unload_percent": (-1, 81, 25.0), "load_hysteresis": (0, 31),
                      "stall_percent": (49, 101), "stall_detect_s": (0.09, 3.01),
                      "reverse_rpm": (4, 31, False), "reverse_duration_s": (0.09, 2.01),
                      "recovery_attempts": (0, 4, 2.0), "typo": (1,)}
        nvm = Nvm()
        store = NvmSettings(nvm)
        for name, values in bad_values.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        store.save_options({name: value})
        with self.assertRaises(ValueError):
            store.save_options({"unload_percent": 80, "load_hysteresis": 21})
        self.assertEqual(nvm.writes, [])
        self.assertEqual(validate_options({"stall_detect_s": 0.4004})["stall_detect_s"], 0.4)

    def test_short_and_readonly_storage_never_claim_success(self):
        short = NvmSettings(Nvm(rpm_record()))
        self.assertEqual(short.default_rpm, 104)
        with self.assertRaisesRegex(SettingsError, "28 bytes"):
            short.save_options({})
        memory = MemorySettings(104)
        self.assertEqual(memory.values, DEFAULT_OPTIONS)
        with self.assertRaises(SettingsError):
            memory.save_options({})
        nvm = Nvm()
        NvmSettings(nvm).save_options({"autostop_enabled": True})
        with patch.dict(sys.modules, {"microcontroller": SimpleNamespace(nvm=bytes(nvm.data))}):
            readonly = open_settings()
        self.assertFalse(readonly.persistent)
        self.assertEqual(readonly.default_rpm, 104)
        self.assertTrue(readonly.values["autostop_enabled"])
        with self.assertRaises(SettingsError):
            readonly.save_options({})


if __name__ == "__main__":
    unittest.main()
