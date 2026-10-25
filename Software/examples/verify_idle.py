"""Bounded on-device API check. Never calls start() or selects a positive RPM.

Run from the CircuitPython REPL after installing grinder_api in /lib. Print
results only after releasing CAN to avoid blocking reception with USB output.
"""

import json
import time

from grinder_api import GrinderController
from grinder_api.circuitpython import open_transport


def _until(controller, predicate, timeout, allow_fault=False):
    deadline = time.monotonic() + timeout
    next_status = 0
    while time.monotonic() < deadline:
        controller.update()
        if controller.fault and not allow_fault:
            raise RuntimeError(controller.fault)
        if time.monotonic() >= next_status:
            status = controller.snapshot()
            next_status = time.monotonic() + 0.02
            if predicate(status):
                return status
        time.sleep(0.001)
    raise RuntimeError("Idle API check timed out: " + str(controller.snapshot()))


def main():
    controller = GrinderController(open_transport())
    before_close = configured = None
    max_gap, updates = 0, 0
    try:
        controller.connect()
        configured = _until(controller, lambda status: status["ready"], 5)
        until = time.monotonic() + 10
        last_update = time.monotonic()
        max_gap, updates = 0, 0
        while time.monotonic() < until:
            now = time.monotonic()
            max_gap = max(max_gap, now - last_update)
            last_update = now
            updates += 1
            controller.update()
            if controller.fault:
                raise RuntimeError(controller.fault)
            time.sleep(0.001)
        before_close = controller.snapshot()
        if not before_close["ready"] or before_close["axis_state"] != 1:
            raise RuntimeError("Drive did not remain ready in IDLE")
    finally:
        controller.close()
        after_close = _until(controller, lambda status: status["state"] == "closed", 10, allow_fault=True)
        print("API_IDLE_STATUS " + json.dumps(before_close))
        print("API_CLOSED_STATUS " + json.dumps(after_close))
        print("API_IDLE_TIMING " + json.dumps({"max_gap_s": max_gap, "updates": updates,
                                              "setup_diagnostics": configured["diagnostics"] if configured else None,
                                              "steady_rx_overflows": (before_close["diagnostics"]["rx_overflows"] - configured["diagnostics"]["rx_overflows"]) if before_close and configured else None}))
    print("API IDLE CHECK COMPLETE")
    return before_close


if __name__ == "__main__":
    main()
