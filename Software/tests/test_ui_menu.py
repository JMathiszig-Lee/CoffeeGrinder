"""Menu interactions, explicit persistence, and integrated demo automation."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui.model import UiModel
from grinder_ui.settings import NvmSettings
from grinder_ui import panel
from grinder_ui.status_led import state_rgb, AMBER, RED


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.nvm = bytearray(64)
        self.ui = UiModel(settings=NvmSettings(self.nvm), clock=self.clock)

    def advance(self, seconds):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now = min(end, self.clock.now + .02)
            self.ui.update()

    def test_menu_drafts_do_not_apply_until_explicit_save_and_cancel_discards_edit(self):
        menu = self.ui.menu
        menu.open()
        menu.turn(2)
        menu.select()
        self.assertEqual(menu.page, "autostop")
        menu.select()
        menu.turn(1)
        menu.back()
        self.assertFalse(menu.options["autostop_enabled"])
        menu.select()
        menu.turn(1)
        menu.select()
        self.assertTrue(menu.options["autostop_enabled"])
        self.assertFalse(self.ui.options["autostop_enabled"])
        self.assertTrue(menu.dirty)
        self.assertEqual(self.nvm, bytearray(64))
        self.assertTrue(self.ui.save_menu())
        self.assertTrue(self.ui.options["autostop_enabled"])
        self.assertTrue(NvmSettings(self.nvm).values["autostop_enabled"])
        self.assertFalse(menu.dirty)

    def test_main_rpm_default_remains_separate_and_menu_tracks_saved_value(self):
        self.ui.turn(-6)
        self.assertTrue(self.ui.save_default())
        self.assertEqual(self.ui.menu.rpm, 104)
        self.assertEqual(self.ui.view()["default_rpm"], 104)
        self.assertEqual(panel.presentation(self.ui.view())["button_a"][0], "MENU")
        self.assertEqual(panel.presentation(self.ui.view())["default"][0], "104")

    def test_menu_changes_require_stopped_motor_and_stop_cancels_recovery(self):
        self.ui.toggle_demo()
        self.ui.press()
        self.advance(1)
        self.ui.menu.open()
        self.ui.menu.turn(3)
        self.ui.menu.select()
        self.ui.menu.select()
        self.assertFalse(self.ui.menu.editing)
        self.assertFalse(self.ui.save_menu())
        self.assertIn("Stop", self.ui.view()["notice"])
        self.ui.stop()
        self.advance(1)
        self.ui.menu.select()
        self.assertTrue(self.ui.menu.editing)
        self.assertFalse(self.ui.automation.snapshot()["active"])

    def test_quick_rpm_save_keeps_pending_options_without_restoring_an_old_default(self):
        self.ui.menu.options["autostop_enabled"] = True
        self.ui.turn(-10)
        self.assertTrue(self.ui.save_default())
        self.assertEqual(self.ui.menu.rpm, 100)
        self.assertTrue(self.ui.menu.dirty)
        self.assertTrue(self.ui.save_menu())
        stored = NvmSettings(self.nvm)
        self.assertEqual(stored.default_rpm, 100)
        self.assertTrue(stored.values["autostop_enabled"])

    def test_target_change_while_resuming_is_sent_to_the_forward_backend(self):
        self.ui.menu.options["stall_recovery_enabled"] = True
        self.assertTrue(self.ui.save_menu())
        self.ui.demo_action("run_demo")
        self.advance(5)
        self.ui.demo_action("jam")
        for _ in range(300):
            self.advance(.02)
            if self.ui.automation.snapshot()["phase"] == "resuming":
                break
        self.assertEqual(self.ui.automation.snapshot()["phase"], "resuming")
        self.ui.turn(-60)
        self.advance(5)
        self.assertEqual(self.ui.view()["set_rpm"], 50)
        self.assertEqual(self.ui.backend.target_rpm, 50)
        self.assertAlmostEqual(self.ui.view()["actual_rpm"], 50)
        self.assertIsNone(self.ui.view()["fault"])

    def test_recovery_scroll_and_menu_readouts_keep_rpm_load_and_fault_visible(self):
        self.ui.menu.open()
        self.ui.menu.turn(3)
        self.ui.menu.select()
        self.ui.menu.turn(5)
        view = self.ui.view()
        self.assertEqual(view["menu"]["rows"][-1][0], "MAX ATTEMPTS")
        self.assertEqual(view["menu"]["position"], "6/6")
        formatted = panel.menu_presentation(view)
        self.assertIn("RPM ---", formatted["menu_rpm"][0])
        self.assertIn("--", formatted["menu_percent"][0])
        self.ui.set_input_error("Encoder disconnected")
        formatted = panel.menu_presentation(self.ui.view())
        self.assertEqual(formatted["menu_status"][0], "INPUT FAULT")

    def test_demo_cycle_autostops_and_does_not_restart(self):
        self.ui.menu.options["autostop_enabled"] = True
        self.ui.menu.options["autostop_delay_s"] = 1.0
        self.assertTrue(self.ui.save_menu())
        self.assertTrue(self.ui.demo_action("run_demo"))
        self.advance(16)
        self.assertEqual(self.ui.view()["state"], "idle")
        self.assertEqual(self.ui.automation.snapshot()["phase"], "complete")
        self.advance(5)
        self.assertEqual(self.ui.view()["actual_rpm"], 0)
        self.assertIsNone(self.ui.view()["fault"])

    def test_demo_jam_reverses_and_resumes_then_stop_prevents_delayed_start(self):
        self.ui.menu.options["stall_recovery_enabled"] = True
        self.assertTrue(self.ui.save_menu())
        self.assertTrue(self.ui.demo_action("run_demo"))
        self.advance(5)
        self.ui.demo_action("jam")
        seen_reverse = False
        seen_stopping = False
        for _ in range(800):
            self.advance(.02)
            view = self.ui.view()
            seen_reverse |= view["actual_rpm"] is not None and view["actual_rpm"] < -5
            seen_stopping |= view["automation"]["phase"] == "stopping_reverse"
            self.assertIsNone(view["fault"])
            if seen_reverse and view["actual_rpm"] > 100:
                break
        self.assertTrue(seen_stopping)
        self.assertTrue(seen_reverse)
        self.assertGreater(self.ui.view()["actual_rpm"], 100)
        self.assertEqual(self.ui.view()["automation"]["attempts"], 1)
        self.ui.demo_action("jam")
        for _ in range(200):
            self.advance(.02)
            if self.ui.view()["automation"]["phase"] == "stopping_reverse":
                break
        self.ui.stop()
        self.advance(10)
        self.assertEqual(self.ui.view()["state"], "idle")
        self.assertFalse(self.ui.view()["automation"]["active"])

    def test_recovery_and_countdown_leds_and_negative_rpm_are_explicit(self):
        view = self.ui.view()
        view.update(mode="simulation", state="running", actual_rpm=-20, load_percent=30,
                    automation={"phase": "reverse", "message": "REVERSING 1/2"})
        rendered = panel.presentation(view)
        self.assertEqual(rendered["actual"][0], "-20")
        self.assertEqual(rendered["status"][0], "STALL RECOVERY")
        self.assertEqual(state_rgb(view, 1), AMBER)
        view["input_error"] = "Encoder disconnected"
        self.assertEqual(state_rgb(view, 1), RED)
        self.assertEqual(panel.presentation(view)["status"][0], "INPUT FAULT")


if __name__ == "__main__":
    unittest.main()
