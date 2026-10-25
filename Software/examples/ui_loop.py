"""CircuitPython UI-loop example; default inputs never request motion.

Install grinder_api in CIRCUITPY/lib and run main() explicitly from the REPL.
Replace read_input_events() with a non-blocking encoder/button adapter.
"""

import time

from grinder_api import GrinderController
from grinder_api.circuitpython import open_transport


MAX_RPM = 110
RPM_STEP = 5
UI_INTERVAL_SECONDS = 0.1


def on_encoder(controller, steps):
    """Adjust the selected RPM; changing the target does not start the motor."""
    selected = controller.snapshot()["target_rpm"] + steps * RPM_STEP
    controller.set_rpm(max(0, min(MAX_RPM, selected)))


def on_start(controller):
    """Call only for an explicit user start event."""
    try:
        controller.start()
    except (RuntimeError, ValueError) as error:
        print("Start refused:", error)


def on_stop(controller):
    """Request IDLE/coasting; the main loop continues until stop confirmation."""
    controller.stop()


def read_input_events():
    """Return (encoder_steps, start_pressed, stop_pressed) without waiting.

    Debounce physical inputs in your adapter. Interrupt handlers should queue
    events for this loop instead of calling the controller directly.
    """
    return 0, False, False


def render_status(status):
    """Replace with a short display update; this example prints at 10 Hz."""
    print(
        "state=", status["state"],
        "target_rpm=", status["target_rpm"],
        "measured_rpm=", status["measured_rpm"],
        "load_percent=", status["load_percent"],
        "telemetry_valid=", status["telemetry_valid"],
        "fault=", status["fault"],
    )


def main():
    controller = GrinderController(
        open_transport(),
        node_id=1,
        max_rpm=MAX_RPM,
        current_limit_a=0.5,
        gear_ratio=8,
        clock=time,
    )
    next_ui = time.monotonic()
    try:
        controller.connect()
        while True:
            controller.update()

            steps, start_pressed, stop_pressed = read_input_events()
            if steps:
                on_encoder(controller, steps)
            # Stop takes priority if both buttons are reported in one update.
            if stop_pressed:
                on_stop(controller)
            elif start_pressed:
                on_start(controller)

            now = time.monotonic()
            if now >= next_ui:
                render_status(controller.snapshot())
                next_ui = now + UI_INTERVAL_SECONDS

            # Keep all input, rendering and logging work short enough to allow
            # update() every 1-5 ms. A long render can delay watchdog feeding.
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        # close() is asynchronous. Keep servicing feedback and stopping until
        # the controller confirms stationary IDLE and closes its transport.
        previous = None
        while controller.snapshot()["state"] != "closed":
            controller.update()
            status = controller.snapshot()
            transition = (status["state"], status["fault"], status["stop_confirmed"])
            if transition != previous:
                print("Closing:", transition)
                previous = transition
            time.sleep(0.001)
        print("Controller closed after confirmed stop.")


if __name__ == "__main__":
    main()
