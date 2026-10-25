"""Pico Display 2.8 application. Startup is OFFLINE; CAN is never initialized.

Turn: set RPM. Push: run/stop (requires demo/live backend).
A: menu/back. B: stop. X: recall default/select. Hold Y 1 s: save.
"""

import time

from .hardware import open_display, open_encoder
from .inputs import Controls
from .model import UiModel
from .render import InstrumentDisplay
from .settings import open_settings
from .status_led import open_status_led


def _buttons():
    import board
    import digitalio
    result = []
    try:
        for name in ("GP12", "GP13", "GP14", "GP15"):
            pin = digitalio.DigitalInOut(getattr(board, name))
            pin.switch_to_input(pull=digitalio.Pull.UP)
            result.append(pin)
    except BaseException:
        for pin in result:
            pin.deinit()
        raise
    return result


def main(run_seconds=None, model=None):
    """Run the panel; optional duration is for bounded, motor-free UI checks."""
    display = open_display()
    renderer = InstrumentDisplay(display)
    model = UiModel(settings=open_settings()) if model is None else model
    pins, encoder, status_led = [], None, None
    renderer.update(model.view())
    try:
        status_led = open_status_led()
        status_led.update(model.view())
        pins = _buttons()
        try:
            encoder = open_encoder()
        except Exception as error:
            model.set_input_error("Encoder: " + str(error))
        raw_buttons = tuple(not pin.value for pin in pins)
        controls = Controls(model, encoder.position if encoder else 0,
                            encoder.pressed if encoder else False, raw_buttons, time.monotonic())
        next_frame = started = time.monotonic()
        print("GRINDER UI READY: CAN OFFLINE; default=%d; encoder=%s" %
              (model.default_rpm, "OK" if encoder else "FAULT"))
        while run_seconds is None or time.monotonic() - started < run_seconds:
            now = time.monotonic()
            if encoder is not None:
                try:
                    position, pressed = encoder.read()
                except Exception as error:
                    model.set_input_error("Encoder: " + str(error))
                    encoder.close()
                    encoder = None
                    position, pressed = controls.position, False
            else:
                position, pressed = controls.position, False
            controls.update(position, pressed, tuple(not pin.value for pin in pins), now)
            model.update()
            if now >= next_frame:
                view = model.view()
                renderer.update(view)
                status_led.update(view)
                next_frame = time.monotonic() + 0.1
            time.sleep(0.001)
        return {"view": model.view(), "bounds": renderer.bounds()}
    finally:
        model.stop()
        # A supplied live supervisor keeps owning its stop/close handshake.
        model.backend.close()
        if model.mode == "live":
            while model.backend.snapshot()["state"] != "closed":
                model.update()
                view = model.view()
                renderer.update(view)
                if status_led is not None:
                    status_led.update(view)
                time.sleep(0.001)
        else:
            model.update()
            renderer.update(model.view())
        if encoder is not None:
            encoder.close()
        for pin in pins:
            pin.deinit()
        if status_led is not None:
            status_led.close()
