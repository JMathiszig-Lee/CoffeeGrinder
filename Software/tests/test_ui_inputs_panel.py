"""Interaction timing and display semantics that must hold without hardware."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui.inputs import Controls
from grinder_ui.model import UiModel
from grinder_ui import panel


class InputTests(unittest.TestCase):
    def setUp(self):
        self.model = UiModel()
        self.controls = Controls(self.model)

    def feed(self, now, pos=0, pressed=False, buttons=(False,) * 4):
        self.controls.update(pos, pressed, buttons, now)

    def test_turning_updates_the_setpoint_without_starting(self):
        self.feed(.01, -3)
        self.assertEqual(self.model.view()["set_rpm"], 107)
        self.assertEqual(self.model.view()["state"], "offline")

    def test_switch_bounce_opens_menu_once_without_starting_demo(self):
        self.feed(.01, buttons=(True, False, False, False))
        self.feed(.02)
        self.feed(.025, buttons=(True, False, False, False))
        self.feed(.06, buttons=(True, False, False, False))
        self.assertEqual(self.model.menu.page, "settings")
        self.assertEqual(self.model.mode, "offline")
        self.feed(.1, buttons=(True, False, False, False))
        self.assertEqual(self.model.menu.page, "settings")

    def test_stop_held_suppresses_start_and_other_buttons(self):
        self.model.toggle_demo()
        self.model.press()
        held = (True, True, False, False)
        self.feed(.01, pressed=True, buttons=held)
        self.feed(.05, pressed=True, buttons=held)
        self.assertEqual(self.model.mode, "simulation")
        self.assertNotEqual(self.model.view()["state"], "running")

    def test_stop_first_raw_level_cancels_automation_without_waiting_for_debounce(self):
        self.model.toggle_demo()
        self.model.press()
        self.assertTrue(self.model.automation.snapshot()["active"])
        self.feed(.001, buttons=(False, True, False, False))
        self.assertFalse(self.model.automation.snapshot()["active"])
        self.assertNotEqual(self.model.view()["state"], "running")

    def test_simultaneous_select_buttons_cannot_start_an_unseen_demo_menu_action(self):
        self.model.menu.open()
        self.feed(.01, pressed=True, buttons=(False, False, True, False))
        self.feed(.05, pressed=True, buttons=(False, False, True, False))
        self.assertEqual(self.model.menu.page, "demo")
        self.assertEqual(self.model.mode, "offline")
        self.assertFalse(self.model.automation.snapshot()["active"])

    def test_default_save_requires_full_hold_and_fires_once(self):
        calls = []
        self.model.save_default = lambda: calls.append(True)
        pressed = (False, False, False, True)
        self.feed(.01, buttons=pressed)
        self.feed(.05, buttons=pressed)
        self.feed(.9, buttons=pressed)
        self.assertEqual(calls, [])
        self.feed(1.06, buttons=pressed)
        self.feed(2.0, buttons=pressed)
        self.assertEqual(calls, [True])

    def test_counter_reset_stops_and_flags_input(self):
        self.feed(.01, pos=100000)
        self.assertIsNotNone(self.model.view()["input_error"])

    def test_holding_encoder_during_boot_does_not_start(self):
        self.model.toggle_demo()
        control = Controls(self.model, pressed=True)
        control.update(0, True, (False,) * 4, .1)
        self.assertEqual(self.model.view()["state"], "idle")


class PresentationTests(unittest.TestCase):
    def test_offline_never_looks_like_zero_speed_or_zero_load(self):
        view = panel.presentation(UiModel().view())
        self.assertEqual(view["actual"][0], "---")
        self.assertIn("--", view["percent"][0])
        self.assertEqual(view["mode"][0], "CAN OFFLINE")
        self.assertIsNone(view["actual_pointer"])
        self.assertIsNone(view["actual_sector"])

    def test_simulation_has_permanent_mode_label(self):
        model = UiModel()
        model.toggle_demo()
        self.assertEqual(panel.presentation(model.view())["mode"][0], "SIMULATION")

    def test_faults_and_high_load_have_text_as_well_as_colour(self):
        view = UiModel().view()
        view.update(mode="simulation", state="running", actual_rpm=98, load_percent=88)
        display = panel.presentation(view)
        self.assertIn("HIGH", display["percent"][0])
        self.assertEqual(display["load_colour"], panel.AMBER)
        view["fault"] = "Lost feedback"
        self.assertEqual(panel.presentation(view)["status"], ("DRIVE FAULT", panel.RED))

    def test_tick_marks_and_pointers_fit_the_screen(self):
        for rpm in range(121):
            for selected in (False, True):
                for x, y in panel.pointer(rpm, selected):
                    self.assertTrue(0 <= x < 320 and 28 <= y < panel.BAR_Y)

    def test_selected_rpm_does_not_drive_the_actual_sector(self):
        view = UiModel().view()
        view.update(mode="simulation", state="running", actual_rpm=70, load_percent=40)
        before = panel.presentation(view)
        view["set_rpm"] = 30
        after = panel.presentation(view)
        self.assertEqual(before["actual_sector"], after["actual_sector"])
        self.assertNotEqual(before["selected_pointer"], after["selected_pointer"])

    def test_filled_sector_area_grows_with_actual_rpm_and_zero_is_distinct(self):
        def area(points):
            if not points:
                return 0
            closed = points + points[:1]
            return abs(sum(a[0] * b[1] - b[0] * a[1]
                           for a, b in zip(closed, closed[1:]))) / 2
        sizes = [area(panel.sector(rpm)) for rpm in range(121)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertGreater(sizes[-1], 10000)
        view = UiModel().view()
        view.update(mode="simulation", state="idle", actual_rpm=0)
        zero = panel.presentation(view)
        self.assertEqual(zero["actual"][0], "000")
        self.assertIsNone(zero["actual_sector"])
        self.assertIsNotNone(zero["actual_pointer"])

    def test_softkeys_align_with_the_four_physical_bezel_buttons(self):
        all_labels = {item[0]: (item[3], item[4]) for item in panel.STATIC_TEXT}
        all_labels.update({item[0]: (item[2], item[3]) for item in panel.DYNAMIC_TEXT})
        self.assertEqual(all_labels["button_a"], (8, 60))
        self.assertEqual(all_labels["button_b"], (8, 180))
        self.assertEqual(all_labels["button_x"], (312, 60))
        self.assertEqual(all_labels["button_y"], (312, 180))
        self.assertNotIn("title", all_labels)

    def test_mode_remains_visible_during_faults_and_saved_default_notices(self):
        view = UiModel().view()
        view.update(mode="simulation", state="running", actual_rpm=100, load_percent=40,
                    notice="Saved default: 100 RPM")
        display = panel.presentation(view)
        self.assertEqual(display["mode"][0], "SIMULATION")
        self.assertIn("SAVED", display["notice"][0])
        view["fault"] = "Lost feedback"
        display = panel.presentation(view)
        self.assertEqual(display["mode"][0], "SIMULATION")
        self.assertIsNone(display["actual_sector"])
        self.assertEqual(display["load_width"], 0)


if __name__ == "__main__":
    unittest.main()
