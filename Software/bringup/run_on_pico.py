"""Run the one-shot commissioning program through the Pico's USB raw REPL."""

import argparse
import csv
from pathlib import Path
import time

import serial

RAW_PROMPT = b"raw REPL; CTRL-B to exit\r\n>"
ACTIONS = ("inspect", "move", "sweep110", "fixture_load")


def capture_program(console, seconds):
    capture = bytearray()

    def drain(deadline):
        while time.monotonic() < deadline:
            data = console.read(4096)
            if data:
                capture.extend(data)
                print(data.decode("utf-8", errors="replace"), end="", flush=True)
            if capture.count(b"\x04") >= 2 and capture.endswith(b"\x04>"):
                return True
        return False

    try:
        finished = drain(time.monotonic() + seconds)
    except KeyboardInterrupt:
        finished = False
    if not finished:
        print("\nStopping Pico program; waiting for device-side IDLE cleanup.", flush=True)
        console.write(b"\x03")
        drain(time.monotonic() + 12)
    return capture


def _target(port, volume, board_id):
    if not isinstance(port, str) or not port.strip() or any(char in port for char in "\x00\r\n"):
        raise ValueError("An explicit serial port is required (--port)")
    if (not isinstance(board_id, str) or not board_id.strip()
            or any(char in board_id for char in "\x00\r\n")):
        raise ValueError("An explicit expected board UID is required (--board-id)")
    if not isinstance(volume, (str, Path)) or (isinstance(volume, str) and not volume.strip()):
        raise ValueError("An explicit CircuitPython volume is required (--volume)")
    if "\x00" in str(volume):
        raise ValueError("CircuitPython volume must be a valid path")
    return port.strip(), Path(volume), board_id.strip()


def _verify_identity(identity, board_id):
    fields = {"Board ID": [], "UID": []}
    for line in identity.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in fields:
            fields[key.strip()].append(value.strip())
    if fields["Board ID"] != ["raspberry_pi_pico2"] or fields["UID"] != [board_id]:
        raise RuntimeError("Unexpected board identity; expected Board ID raspberry_pi_pico2 and UID " + board_id)


def run(action, fixture_ready=False, *, port=None, volume=None, board_id=None):
    if action == "fixture_load" and not fixture_ready:
        raise RuntimeError("Secure the brake fixture, release its load and keep hands clear before using --fixture-ready")
    if action not in ACTIONS:
        raise ValueError("Unknown commissioning action: " + str(action))
    port, volume, board_id = _target(port, volume, board_id)
    directory = Path(__file__).resolve().parent
    root = directory.parents[1]
    identity = (volume / "boot_out.txt").read_text()
    _verify_identity(identity, board_id)
    if (volume / "code.py").read_bytes() != (directory / "code.py").read_bytes():
        raise RuntimeError("Passive code.py prerequisite differs from the reviewed file")
    program = "__name__ = 'commissioning'\n" + (directory / "motor_test.py").read_text()
    if action in ("sweep110", "fixture_load"):
        program += "\n" + (directory / "conical_sweep.py").read_text()
        if action == "fixture_load":
            program += "\n" + (directory / "current_sense.py").read_text()
            program += "\nmain(move=True, trial_type=CurrentSense)\n"
        else:
            program += "\nmain(move=True, trial_type=ConicalSweep)\n"
    else:
        program += "\nmain(move=%s)\n" % (action == "move")
    payload = program.encode()
    with serial.Serial(port, 115200, timeout=1, write_timeout=1, exclusive=True) as console:
        console.dtr = True
        console.write(b"\x02\x03\x03\r\x01")
        preface = console.read_until(RAW_PROMPT)
        if not preface.endswith(RAW_PROMPT):
            console.write(b"\r\x01")
            preface = console.read_until(RAW_PROMPT)
        if not preface.endswith(RAW_PROMPT):
            raise RuntimeError("Could not enter Pico raw REPL")
        console.write(b"\x04")
        if not console.read_until(RAW_PROMPT).endswith(RAW_PROMPT):
            raise RuntimeError("Could not reset raw REPL")
        for offset in range(0, len(payload), 128):
            console.write(payload[offset:offset + 128])
            time.sleep(0.005)
        console.write(b"\x04")
        console.timeout = 0.1
        timeout = 120 if action == "fixture_load" else (90 if action == "sweep110" else 25)
        capture = capture_program(console, timeout)
        console.write(b"\x02")
    destination = root / ".local" / ("motor-%s.log" % action)
    destination.parent.mkdir(exist_ok=True)
    destination.write_bytes(capture)
    print("\nCapture:", destination)
    if action in ("sweep110", "fixture_load"):
        samples = []
        if action == "sweep110":
            prefix = "SWEEP_SAMPLE "
            columns = ("t", "target", "rpm", "position", "iq", "vbus", "ibus")
        else:
            prefix = "CURRENT_SAMPLE "
            columns = ("t", "phase", "target", "rpm", "iq_set", "iq", "iq_mean", "vbus", "ibus")
        for line in capture.decode("utf-8", errors="replace").splitlines():
            if prefix not in line:
                continue
            try:
                fields = dict(item.split("=", 1) for item in line.split(prefix, 1)[1].split())
                samples.append({key: fields[key] if key == "phase" else float(fields[key]) for key in columns})
            except (KeyError, ValueError):
                continue  # Preserve a stopped run even if its final line was partial.
        sample_path = destination.with_suffix(".csv")
        with sample_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(samples)
        print("Samples:", sample_path)
    if b"TEST COMPLETE:" not in capture or b"TEST STOP:" in capture:
        raise RuntimeError("Pico trial did not complete; inspect the capture")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Explicit USB serial port for the target Pico")
    parser.add_argument("--volume", required=True, type=Path,
                        help="Mounted CIRCUITPY directory containing boot_out.txt and passive code.py")
    parser.add_argument("--board-id", required=True,
                        help="Exact expected UID from boot_out.txt; Board ID must be raspberry_pi_pico2")
    parser.add_argument("--action", choices=ACTIONS, default="inspect")
    parser.add_argument("--fixture-ready", action="store_true",
                        help="Fixture-load test only: brake/motor secured, initial load released, hands clear")
    arguments = parser.parse_args(argv)
    return run(arguments.action, fixture_ready=arguments.fixture_ready,
               port=arguments.port, volume=arguments.volume, board_id=arguments.board_id)


if __name__ == "__main__":
    main()
