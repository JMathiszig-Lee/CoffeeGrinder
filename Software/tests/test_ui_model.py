"""UI intent, honest telemetry and eight-byte persistent-default behavior."""

import builtins
import importlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui import model as model_module
from grinder_ui.model import DemoMotor, OfflineMotor, UiModel
from grinder_ui.settings import MemorySettings, NvmSettings, SettingsError, open_settings


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Nvm:
    def __init__(self, record=None):
        self.data = bytearray(bytes(8) if record is None else record)
        self.data.extend(b"keep these bytes")
        self.writes = []
        self.fail = False
        self.drop_write = False

    def __len__(self):
        return len(self.data)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        if self.fail:
            raise OSError("NVM is read-only")
        self.writes.append(bytes(value))
        if not self.drop_write:
            self.data[key] = value


class LiveMotor:
    def __init__(self):
        self.calls = []
        self.status = {"state": "idle", "ready": True, "running": False,
                       "telemetry_valid": True, "measured_rpm": 0.0,
                       "load_valid": False, "load_percent": None, "fault": None}
        self.fail_update = False

    def connect(self):
        self.calls.append(("connect",))

    def set_rpm(self, rpm):
        self.calls.append(("set_rpm", rpm))

    def start(self):
        self.calls.append(("start",))
        self.status.update(state="running", running=True, ready=False,
                           measured_rpm=10.0, load_percent=25.0, load_valid=True)

    def stop(self):
        self.calls.append(("stop",))
        self.status.update(state="stopping", running=False, ready=False)

    def update(self):
        if self.fail_update:
            raise OSError("CAN unavailable")
        self.calls.append(("update",))

    def snapshot(self):
        return dict(self.status)


class UiModelTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.nvm = Nvm()
        self.settings = NvmSettings(self.nvm)
        self.ui = UiModel(settings=self.settings, clock=self.clock)

    def advance(self, seconds):
        self.clock.now += seconds
        self.ui.update()

    def test_default_offline_does_not_fabricate_data_or_start(self):
        view = self.ui.view()
        self.assertEqual((view["mode"], view["state"], view["set_rpm"]), ("offline", "offline", 110))
        self.assertIsNone(view["actual_rpm"])
        self.assertIsNone(view["load_percent"])
        self.assertFalse(self.ui.press())
        self.assertIn("offline", self.ui.view()["notice"])
        self.assertEqual(self.nvm.writes, [])
        json.dumps(self.ui.view(), allow_nan=False)

    def test_model_reload_and_all_offline_demo_actions_have_no_hardware_imports(self):
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("board", "busio", "digitalio", "microcontroller", "grinder_api"):
                raise AssertionError("Unexpected hardware import: " + name)
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guard):
            importlib.reload(model_module)
            ui = model_module.UiModel(clock=self.clock)
            ui.turn(-10)
            ui.press()
            ui.toggle_demo()
            ui.press()
            self.clock.now += 0.2
            ui.update()
            ui.stop()
            ui.toggle_demo()
            self.assertEqual(ui.view()["mode"], "offline")

    def test_demo_is_explicit_stopped_then_slews_and_announces_simulation(self):
        self.ui.toggle_demo()
        view = self.ui.view()
        self.assertEqual((view["mode"], view["state"], view["actual_rpm"]), ("simulation", "idle", 0.0))
        self.assertIsNone(view["load_percent"])
        self.assertIn("simulated", view["notice"])
        self.assertTrue(self.ui.press())
        self.advance(1)
        view = self.ui.view()
        self.assertEqual(view["state"], "running")
        self.assertAlmostEqual(view["actual_rpm"], 25.0)
        self.assertEqual(view["set_rpm"], 110)
        self.assertGreater(view["load_percent"], 0)
        self.ui.press()
        self.assertEqual(self.ui.view()["state"], "stopping")
        self.assertIsNone(self.ui.view()["load_percent"])
        self.advance(1)
        self.assertEqual(self.ui.view()["actual_rpm"], 0)
        self.assertEqual(self.ui.view()["state"], "idle")
        self.ui.toggle_demo()
        self.assertEqual(self.ui.view()["mode"], "offline")
        self.assertIsNone(self.ui.view()["actual_rpm"])
        self.ui.toggle_demo()
        self.assertEqual(self.ui.view()["state"], "idle")

    def test_encoder_clamps_in_single_rpm_steps_and_never_saves(self):
        self.ui.turn(-1)
        self.assertEqual(self.ui.view()["set_rpm"], 109)
        self.ui.turn(-1000)
        self.assertEqual(self.ui.view()["set_rpm"], 5)
        self.ui.turn(1000)
        self.assertEqual(self.ui.view()["set_rpm"], 110)
        self.assertEqual(self.ui.view()["default_rpm"], 110)
        self.assertEqual(self.nvm.writes, [])
        for value in (True, 1.5, "1"):
            with self.assertRaises(ValueError):
                self.ui.turn(value)

    def test_default_changes_only_on_explicit_save_and_recall_does_not_start(self):
        self.ui.turn(-30)
        self.ui.press()
        self.assertEqual(self.ui.view()["default_rpm"], 110)
        self.assertTrue(self.ui.save_default())
        self.assertEqual(self.ui.view()["default_rpm"], 80)
        self.assertEqual(NvmSettings(self.nvm).default_rpm, 80)
        self.ui.toggle_demo()
        self.ui.turn(10)
        self.ui.recall_default()
        self.assertEqual(self.ui.view()["set_rpm"], 80)
        self.assertEqual(self.ui.view()["state"], "idle")
        self.assertEqual(len(self.nvm.writes), 1)
        self.ui.save_default()
        self.assertEqual(len(self.nvm.writes), 1)

    def test_live_backend_is_not_connected_or_commanded_until_input(self):
        motor = LiveMotor()
        ui = UiModel(motor, self.settings, self.clock)
        self.assertEqual(motor.calls, [])
        self.assertEqual(ui.view()["mode"], "live")
        ui.turn(-10)
        self.assertEqual(motor.calls, [("set_rpm", 100)])
        self.assertTrue(ui.press())
        self.assertEqual(motor.calls[-2:], [("set_rpm", 100), ("start",)])
        ui.press()
        self.assertEqual(motor.calls[-1], ("stop",))
        self.assertFalse(ui.toggle_demo())
        self.assertIs(ui.backend, motor)
        self.assertEqual(ui.view()["mode"], "live")

    def test_stale_fault_and_nonfinite_telemetry_are_hidden_not_zero(self):
        motor = LiveMotor()
        ui = UiModel(motor, self.settings, self.clock)
        motor.start()
        self.assertEqual(ui.view()["actual_rpm"], 10.0)
        self.assertEqual(ui.view()["load_percent"], 25.0)
        motor.status["telemetry_valid"] = False
        self.assertIsNone(ui.view()["actual_rpm"])
        self.assertIsNone(ui.view()["load_percent"])
        motor.status.update(telemetry_valid=True, fault="Drive fault")
        self.assertEqual(ui.view()["fault"], "Drive fault")
        self.assertIsNone(ui.view()["actual_rpm"])
        self.assertIsNone(ui.view()["load_percent"])
        motor.status.update(fault=None, measured_rpm=float("nan"), load_percent=float("inf"))
        self.assertIsNone(ui.view()["actual_rpm"])
        self.assertIsNone(ui.view()["load_percent"])
        json.dumps(ui.view(), allow_nan=False)

    def test_input_error_latches_stops_backend_and_prevents_restart(self):
        motor = LiveMotor()
        ui = UiModel(motor, self.settings, self.clock)
        ui.press()
        ui.set_input_error("Encoder I2C failed")
        self.assertEqual(motor.calls[-1], ("stop",))
        stops = motor.calls.count(("stop",))
        ui.set_input_error("Another read failed")
        self.assertEqual(motor.calls.count(("stop",)), stops)
        self.assertEqual(ui.view()["input_error"], "Encoder I2C failed")
        self.assertIsNone(ui.view()["actual_rpm"])
        self.assertFalse(ui.press())
        self.assertEqual(motor.calls[-1], ("stop",))
        ui.turn(-1)
        self.assertEqual(motor.calls[-1], ("stop",))

    def test_backend_failure_is_visible_and_requests_stop(self):
        motor = LiveMotor()
        ui = UiModel(motor, self.settings, self.clock)
        motor.fail_update = True
        ui.update()
        self.assertIn("CAN unavailable", ui.view()["fault"])
        self.assertEqual(motor.calls[-1], ("stop",))
        self.assertFalse(ui.press())


class SettingsTests(unittest.TestCase):
    def test_exact_record_roundtrip_preserves_tail_and_unchanged_save_does_not_write(self):
        nvm = Nvm()
        original_tail = bytes(nvm.data[8:])
        store = NvmSettings(nvm)
        self.assertEqual(store.load(), 110)
        self.assertEqual(nvm.writes, [])
        self.assertTrue(store.save(73))
        self.assertEqual(bytes(nvm.data[:8]), b"CGUI" + struct.pack("<HH", 73, 73 ^ 0xFFFF))
        self.assertEqual(bytes(nvm.data[8:]), original_tail)
        self.assertEqual(NvmSettings(nvm).default_rpm, 73)
        self.assertFalse(store.save(73))
        self.assertEqual(len(nvm.writes), 1)

    def test_corruption_is_visible_and_can_be_repaired_but_foreign_record_is_preserved(self):
        for record in (b"CGUI" + struct.pack("<HH", 70, 0),
                       b"CGUI" + struct.pack("<HH", 111, 111 ^ 0xFFFF)):
            with self.subTest(record=record):
                nvm = Nvm(record)
                store = NvmSettings(nvm)
                self.assertEqual(store.default_rpm, 110)
                self.assertIn("corrupt", store.message)
                self.assertTrue(store.save(110))
                self.assertEqual(NvmSettings(nvm).default_rpm, 110)
        nvm = Nvm(b"FOREIGN!")
        store = NvmSettings(nvm)
        with self.assertRaises(SettingsError):
            store.save(80)
        self.assertEqual(bytes(nvm.data[:8]), b"FOREIGN!")
        self.assertEqual(nvm.writes, [])

    def test_both_blank_patterns_are_accepted_and_invalid_values_never_write(self):
        for blank in (bytes(8), b"\xff" * 8):
            nvm = Nvm(blank)
            store = NvmSettings(nvm)
            self.assertIsNone(store.message)
            for invalid in (True, 4, 111, 80.0, "80"):
                with self.assertRaises(ValueError):
                    store.save(invalid)
            self.assertEqual(nvm.writes, [])

    def test_record_changed_by_another_owner_before_save_is_preserved(self):
        nvm = Nvm()
        store = NvmSettings(nvm)
        nvm.data[:8] = b"FOREIGN!"
        with self.assertRaises(SettingsError):
            store.save(90)
        self.assertEqual(bytes(nvm.data[:8]), b"FOREIGN!")
        self.assertEqual(nvm.writes, [])

    def test_failed_or_unverified_save_preserves_model_default_and_reports_failure(self):
        for failure in ("fail", "drop_write"):
            nvm = Nvm()
            setattr(nvm, failure, True)
            store = NvmSettings(nvm)
            ui = UiModel(settings=store)
            ui.turn(-20)
            self.assertFalse(ui.save_default())
            self.assertEqual(ui.view()["default_rpm"], 110)
            self.assertIn("not saved", ui.view()["notice"])

    def test_missing_or_readonly_storage_never_claims_persistence(self):
        original_import = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "microcontroller":
                raise ImportError("NVM absent")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=unavailable):
            store = open_settings()
        self.assertIsInstance(store, MemorySettings)
        self.assertFalse(store.persistent)
        with self.assertRaises(SettingsError):
            store.save(90)
        readonly = NvmSettings(bytes(8))
        with self.assertRaises(SettingsError):
            readonly.save(90)
        self.assertEqual(readonly.default_rpm, 110)
        record = b"CGUI" + struct.pack("<HH", 70, 70 ^ 0xFFFF)
        with patch.dict(sys.modules, {"microcontroller": SimpleNamespace(nvm=record)}):
            fallback = open_settings()
        self.assertIsInstance(fallback, MemorySettings)
        self.assertEqual(fallback.default_rpm, 70)
        self.assertFalse(fallback.persistent)
        with self.assertRaises(SettingsError):
            fallback.save(80)


if __name__ == "__main__":
    unittest.main()
