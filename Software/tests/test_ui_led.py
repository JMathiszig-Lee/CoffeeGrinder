"""Verified common-anode RGB wiring and honest UI state indications."""

import builtins
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grinder_ui import status_led as LED


class Clock:
    def monotonic(self):
        return 1.0

    def sleep(self, seconds):
        raise AssertionError("LED updates must not sleep")


class PWM:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self._duty = 65535
        self.fail_off = False

    @property
    def duty_cycle(self):
        return self._duty

    @duty_cycle.setter
    def duty_cycle(self, value):
        if self.fail_off and value == 65535:
            raise OSError("injected PWM off failure")
        self.events.append(("duty", self.name, value))
        self._duty = value

    def deinit(self):
        self.events.append(("deinit", self.name))


def view(mode="live", state="running", **extra):
    return dict({"mode": mode, "state": state, "actual_rpm": 50.0,
                 "load_percent": 25.0, "fault": None, "input_error": None}, **extra)


class StatusLEDTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.channels = [PWM(name, self.events) for name in ("red", "green", "blue")]
        self.led = LED.StatusLED(self.channels, clock=Clock())

    def test_common_anode_polarity_modest_default_and_redundant_updates(self):
        self.assertTrue(all(channel.duty_cycle == 65535 for channel in self.channels))
        self.assertTrue(self.led.update(view(fault="CAN lost")))
        self.assertEqual([channel.duty_cycle for channel in self.channels],
                         [65535 - round(65535 * 0.08), 65535, 65535])
        writes = len(self.events)
        self.assertFalse(self.led.update(view(fault="CAN lost")))
        self.assertEqual(len(self.events), writes)

    def test_fault_and_input_error_take_priority_over_every_mode_and_load(self):
        for mode in ("offline", "simulation", "live"):
            for error in ({"fault": "Drive fault"}, {"input_error": "Encoder lost"}):
                with self.subTest(mode=mode, error=error):
                    self.assertEqual(LED.state_rgb(view(mode=mode, load_percent=100, **error), 0), LED.RED)
        self.assertEqual(LED.state_rgb(view(state="fault"), 0), LED.RED)

    def test_offline_transitions_idle_running_and_high_load_threshold(self):
        self.assertEqual(LED.state_rgb(view(mode="offline"), 0), LED.AMBER)
        self.assertEqual(LED.state_rgb(view(state="idle"), 0), LED.IDLE_WHITE)
        self.assertEqual(LED.state_rgb(view(load_percent=79.99), 0), LED.GREEN)
        self.assertEqual(LED.state_rgb(view(load_percent=80), 0), LED.AMBER)
        for state in ("connecting", "starting", "stopping"):
            with self.subTest(state=state):
                low = LED.state_rgb(view(state=state), 0)
                high = LED.state_rgb(view(state=state), 1)
                self.assertGreater(high[0], low[0])
                self.assertEqual(high[2], 0)
        self.assertNotEqual(LED.state_rgb(view(actual_rpm=None), 1), LED.GREEN)

    def test_simulation_distinguishes_idle_running_and_transitions_without_live_green(self):
        for state in ("idle", "starting", "running", "stopping"):
            for moment in (0, 0.5, 1, 2):
                with self.subTest(state=state, moment=moment):
                    colour = LED.state_rgb(view(mode="simulation", state=state), moment)
                    self.assertEqual(colour[0], 0)
                    self.assertGreater(colour[2], colour[1])
                    self.assertNotEqual(colour, LED.GREEN)
        self.assertEqual(LED.state_rgb(view(mode="simulation", state="idle"), 0), LED.SIM_IDLE_BLUE)
        self.assertEqual(LED.state_rgb(view(mode="simulation", state="idle"), 1), LED.SIM_IDLE_BLUE)
        self.assertEqual(LED.state_rgb(view(mode="simulation"), 0), LED.CYAN)
        self.assertEqual(LED.state_rgb(view(mode="simulation"), 1), LED.CYAN)
        for state in ("starting", "stopping"):
            self.assertNotEqual(LED.state_rgb(view(mode="simulation", state=state), 0),
                                LED.state_rgb(view(mode="simulation", state=state), 1))

    def test_simulated_high_load_pulses_amber_and_fault_still_wins(self):
        below = LED.state_rgb(view(mode="simulation", load_percent=79.99), 0)
        high_low = LED.state_rgb(view(mode="simulation", load_percent=80), 0)
        high_peak = LED.state_rgb(view(mode="simulation", load_percent=80), 1)
        self.assertEqual(below, LED.CYAN)
        self.assertEqual(high_peak, LED.AMBER)
        self.assertNotEqual(high_low, high_peak)
        self.assertEqual(LED.state_rgb(view(mode="simulation", load_percent=99, fault="Fault"), 0), LED.RED)

    def test_brightness_is_clamped_and_nonfinite_is_rejected(self):
        self.led.brightness = 2
        self.led.update(view(), now=1)
        self.assertEqual([channel.duty_cycle for channel in self.channels], [65535, 0, 65535])
        self.led.brightness = -1
        self.led.update(view(), now=1)
        self.assertTrue(all(channel.duty_cycle == 65535 for channel in self.channels))
        for invalid in (float("nan"), float("inf"), True, "0.08"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                LED.StatusLED(self.channels, brightness=invalid)

    def test_close_turns_every_channel_off_before_any_deinit_and_is_idempotent(self):
        self.led.update(view())
        self.events.clear()
        self.led.close()
        self.assertEqual(self.events[:3], [("duty", name, 65535) for name in ("red", "green", "blue")])
        self.assertEqual(self.events[3:], [("deinit", name) for name in ("red", "green", "blue")])
        self.led.close()
        self.assertEqual(len(self.events), 6)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.led.update(view())

    def test_shutdown_failure_still_deinitializes_all_channels(self):
        self.channels[0].fail_off = True
        with self.assertRaisesRegex(RuntimeError, "off failure"):
            self.led.close()
        self.assertEqual([event[1] for event in self.events if event[0] == "deinit"],
                         ["red", "green", "blue"])

    def test_import_and_pure_state_mapping_never_import_hardware(self):
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name.split(".")[0] in ("board", "pwmio", "busio", "grinder_api"):
                raise AssertionError("Unexpected hardware import: " + name)
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded):
            importlib.reload(LED)
            self.assertEqual(LED.state_rgb(view(mode="offline"), 0), LED.AMBER)

    def test_factory_uses_verified_rgb_gpio_order_and_starts_off(self):
        calls = []

        def pwm_factory(pin, **options):
            calls.append((pin, options))
            return PWM(pin, self.events)

        modules = {"board": SimpleNamespace(GP26=26, GP27=27, GP28=28),
                   "pwmio": SimpleNamespace(PWMOut=pwm_factory)}
        with patch.dict(sys.modules, modules):
            led = LED.open_status_led()
        self.assertEqual(calls, [(pin, {"frequency": 1000, "duty_cycle": 65535})
                                 for pin in (26, 27, 28)])
        self.assertEqual(led.brightness, 0.08)


if __name__ == "__main__":
    unittest.main()
