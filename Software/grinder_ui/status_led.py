"""Pico Display Pack 2.8 status light; imports never access hardware.

Red: fault. Amber: offline or high load; gently pulsing amber: transition or
unavailable live RPM. Dim white: live idle. Green: live running; amber warns
at 80% load. Simulation uses dim blue when idle, cyan when running, pulsing
cyan during transitions, and pulsing amber at high load; its screen label
continues to identify the simulation mode.

Pimoroni PicoDisplay28.hpp: R=GP26, G=GP27, B=GP28. The PIM715 schematic
connects the common anode to 3V3: PWM duty 65535 is off on every channel.
"""

import math
import time

RED = (1.0, 0.0, 0.0)
AMBER = (1.0, 0.32, 0.0)
GREEN = (0.0, 1.0, 0.0)
CYAN = (0.0, 0.55, 1.0)
SIM_IDLE_BLUE = (0.0, 0.0, 0.25)
IDLE_WHITE = (0.15, 0.15, 0.15)
OFF = (0.0, 0.0, 0.0)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _clamp(value):
    if not _finite(value):
        raise ValueError("LED brightness must be a finite number")
    return max(0.0, min(1.0, float(value)))


def _pulse(colour, now):
    if not _finite(now):
        raise ValueError("LED update time must be finite")
    intensity = 0.35 + 0.325 * (1.0 - math.cos(math.pi * now))
    return tuple(component * intensity for component in colour)


def state_rgb(view, now):
    """Pure state/colour mapping with a smooth two-second pulse where needed."""
    mode = str(view.get("mode", "offline")).lower()
    state = str(view.get("state", "offline")).lower()
    if view.get("fault") or view.get("input_error") or state == "fault":
        return RED
    automation = view.get("automation") or {}
    if automation.get("phase") in ("stopping_reverse", "reverse", "stopping_resume", "resuming"):
        return _pulse(AMBER, now)
    if automation.get("remaining_s") is not None:
        return _pulse(CYAN if mode == "simulation" else GREEN, now)
    if mode == "simulation":
        if state == "idle":
            return SIM_IDLE_BLUE
        if state == "running":
            load = view.get("load_percent")
            return _pulse(AMBER, now) if _finite(load) and load >= 80 else CYAN
        return _pulse(CYAN, now)
    if mode != "live" or state in ("offline", "disconnected", "closed"):
        return AMBER
    if state in ("connecting", "starting", "stopping"):
        return _pulse(AMBER, now)
    if not _finite(view.get("actual_rpm")):
        return _pulse(AMBER, now)
    if state == "idle":
        return IDLE_WHITE
    if state == "running":
        load = view.get("load_percent")
        return AMBER if _finite(load) and load >= 80 else GREEN
    return _pulse(AMBER, now)


class StatusLED:
    """Three common-anode PWM channels in RGB order; updates never sleep."""

    def __init__(self, channels, clock=time, brightness=0.08):
        if len(channels) != 3:
            raise ValueError("StatusLED requires red, green and blue PWM channels")
        self.channels = tuple(channels)
        self.clock = clock
        self.brightness = _clamp(brightness)
        self._last = None
        self._closed = False
        self._apply(OFF)

    def _apply(self, colour):
        brightness = _clamp(self.brightness)
        duties = tuple(65535 - round(65535 * brightness * _clamp(component))
                       for component in colour)
        if duties == self._last:
            return False
        for channel, duty in zip(self.channels, duties):
            channel.duty_cycle = duty
        self._last = duties
        return True

    def update(self, view, now=None):
        if self._closed:
            raise RuntimeError("Status LED is closed")
        return self._apply(state_rgb(view, self.clock.monotonic() if now is None else now))

    def close(self):
        if self._closed:
            return
        self._closed = True
        failure = None
        # Turn all channels off before releasing any PWM resource.
        for channel in self.channels:
            try:
                channel.duty_cycle = 65535
            except Exception as error:
                failure = error
        for channel in self.channels:
            try:
                channel.deinit()
            except Exception as error:
                failure = error
        if failure is not None:
            raise RuntimeError("Status LED close failed: " + str(failure))


def open_status_led(*, brightness=0.08, clock=time):
    """Open the verified PIM715 RGB pins, initially off; no CAN is opened."""
    brightness = _clamp(brightness)
    import board
    import pwmio

    channels = []
    try:
        for name in ("GP26", "GP27", "GP28"):
            channels.append(pwmio.PWMOut(getattr(board, name), frequency=1000, duty_cycle=65535))
        return StatusLED(channels, clock=clock, brightness=brightness)
    except BaseException:
        for channel in channels:
            try:
                channel.duty_cycle = 65535
            except Exception:
                pass
        for channel in channels:
            try:
                channel.deinit()
            except Exception:
                pass
        raise
