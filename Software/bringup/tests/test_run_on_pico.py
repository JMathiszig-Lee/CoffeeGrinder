"""Explicit targeting and fixture gates must pass before any device access."""

from contextlib import ExitStack, redirect_stderr
import ast
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "pico_runner", Path(__file__).resolve().parents[1] / "run_on_pico.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class FixtureReadinessTests(unittest.TestCase):
    def test_no_fixture_readiness_means_no_device_or_volume_access(self):
        with patch.object(RUNNER.serial, "Serial") as serial_port:
            with patch.object(RUNNER.Path, "read_text") as read_volume:
                with self.assertRaisesRegex(RuntimeError, "Secure the brake fixture"):
                    RUNNER.run("fixture_load")
                serial_port.assert_not_called()
                read_volume.assert_not_called()

    def test_host_interrupt_reaches_pico_and_keeps_reading_stop_confirmation(self):
        class Console:
            def __init__(self):
                self.reads = 0
                self.sent = []

            def read(self, length):
                self.reads += 1
                if self.reads == 1:
                    raise KeyboardInterrupt
                return b"COAST STOP CONFIRMED\r\nSAFE IDLE\x04\x04>"

            def write(self, data):
                self.sent.append(data)

        console = Console()
        with patch("builtins.print"):
            result = RUNNER.capture_program(console, 1)
        self.assertEqual(console.sent, [b"\x03"])
        self.assertIn(b"COAST STOP CONFIRMED", result)
        self.assertEqual(console.reads, 2)


class TargetValidationTests(unittest.TestCase):
    UID = "0123456789ABCDEF"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.volume = Path(temporary.name)
        self.targets = {"port": "/dev/test-pico", "volume": self.volume, "board_id": self.UID}
        self.passive = (Path(__file__).resolve().parents[1] / "code.py").read_bytes()
        (self.volume / "code.py").write_bytes(self.passive)
        self.identity()

    def identity(self, board="raspberry_pi_pico2", uid=UID, extra=""):
        (self.volume / "boot_out.txt").write_text(
            "Adafruit CircuitPython 9.2.8; Raspberry Pi Pico 2 with rp2350\n"
            "Board ID: %s\nUID: %s\n%s" % (board, uid, extra))

    def test_missing_or_invalid_targets_fail_before_file_or_serial_access(self):
        cases = ({}, {"port": "/dev/test-pico"},
                 {"port": "/dev/test-pico", "volume": self.volume},
                 dict(self.targets, port=""), dict(self.targets, port=True),
                 dict(self.targets, port="/dev/test\nother"),
                 dict(self.targets, volume=None), dict(self.targets, volume=" "),
                 dict(self.targets, volume=True), dict(self.targets, volume="bad\x00path"),
                 dict(self.targets, board_id=""), dict(self.targets, board_id=True),
                 dict(self.targets, board_id=self.UID + "\nother"))
        with patch.object(RUNNER.Path, "read_text") as read_text, \
                patch.object(RUNNER.Path, "read_bytes") as read_bytes, \
                patch.object(RUNNER.serial, "Serial") as serial_port:
            for targets in cases:
                with self.subTest(targets=targets), self.assertRaises(ValueError):
                    RUNNER.run("inspect", **targets)
            read_text.assert_not_called()
            read_bytes.assert_not_called()
            serial_port.assert_not_called()

    def test_invalid_action_fails_before_file_or_serial_access(self):
        with patch.object(RUNNER.Path, "read_text") as read_text, \
                patch.object(RUNNER.Path, "read_bytes") as read_bytes, \
                patch.object(RUNNER.serial, "Serial") as serial_port:
            with self.assertRaisesRegex(ValueError, "Unknown commissioning action"):
                RUNNER.run("unrecognised", **self.targets)
            read_text.assert_not_called()
            read_bytes.assert_not_called()
            serial_port.assert_not_called()

    def test_wrong_or_substring_uid_is_rejected_before_serial(self):
        for uid in ("FEDCBA9876543210", self.UID + "0", "0" + self.UID):
            with self.subTest(uid=uid):
                self.identity(uid=uid)
                with patch.object(RUNNER.serial, "Serial") as serial_port:
                    with self.assertRaisesRegex(RuntimeError, "Unexpected board identity"):
                        RUNNER.run("inspect", **self.targets)
                    serial_port.assert_not_called()

    def test_wrong_missing_or_duplicate_platform_identity_is_rejected(self):
        identities = (
            "Board ID: raspberry_pi_pico2_w\nUID: " + self.UID,
            "Raspberry Pi Pico 2\nUID: " + self.UID,
            "Board ID: raspberry_pi_pico2\nUID: " + self.UID + "\nUID: " + self.UID,
            "Board ID: raspberry_pi_pico2\nBoard ID: raspberry_pi_pico2\nUID: " + self.UID,
        )
        for identity in identities:
            with self.subTest(identity=identity):
                (self.volume / "boot_out.txt").write_text(identity)
                with patch.object(RUNNER.serial, "Serial") as serial_port:
                    with self.assertRaisesRegex(RuntimeError, "Unexpected board identity"):
                        RUNNER.run("inspect", **self.targets)
                    serial_port.assert_not_called()

    def test_startup_mismatch_is_rejected_before_serial(self):
        (self.volume / "code.py").write_text("print('different startup')\n")
        with patch.object(RUNNER.serial, "Serial") as serial_port:
            with self.assertRaisesRegex(RuntimeError, "Passive code.py prerequisite differs"):
                RUNNER.run("inspect", **self.targets)
            serial_port.assert_not_called()

    def test_inspect_uploads_only_nonmotion_entrypoint_to_explicit_port(self):
        class Console:
            def __init__(self):
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read_until(self, expected):
                return RUNNER.RAW_PROMPT

            def write(self, data):
                self.sent.append(bytes(data))

        console = Console()
        with ExitStack() as stack:
            serial_port = stack.enter_context(patch.object(RUNNER.serial, "Serial", return_value=console))
            stack.enter_context(patch.object(RUNNER, "capture_program", return_value=b"TEST COMPLETE: inspect\n"))
            stack.enter_context(patch.object(RUNNER.time, "sleep"))
            stack.enter_context(patch.object(RUNNER.Path, "mkdir"))
            log_write = stack.enter_context(patch.object(RUNNER.Path, "write_bytes"))
            stack.enter_context(patch("builtins.print"))
            RUNNER.run("inspect", **self.targets)
        serial_port.assert_called_once_with("/dev/test-pico", 115200, timeout=1,
                                            write_timeout=1, exclusive=True)
        uploaded = b"".join(console.sent)
        self.assertIn(b"__name__ = 'commissioning'\n", uploaded)
        self.assertIn(b"main(move=False)\n\x04\x02", uploaded)
        program = b"".join(console.sent[2:-2]).decode()
        calls = [node.value for node in ast.parse(program).body
                 if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and isinstance(node.value.func, ast.Name) and node.value.func.id == "main"]
        self.assertEqual(len(calls), 1)
        self.assertEqual([(keyword.arg, ast.literal_eval(keyword.value))
                          for keyword in calls[0].keywords], [("move", False)])
        log_write.assert_called_once_with(b"TEST COMPLETE: inspect\n")

    def test_cli_requires_each_target_and_defaults_to_inspect(self):
        flags = ["--port", "/dev/test-pico", "--volume", str(self.volume), "--board-id", self.UID]
        with patch.object(RUNNER, "run") as run:
            RUNNER.main(flags)
        run.assert_called_once_with("inspect", fixture_ready=False, port="/dev/test-pico",
                                    volume=self.volume, board_id=self.UID)
        for start in (0, 2, 4):
            incomplete = flags[:start] + flags[start + 2:]
            with self.subTest(omitted=flags[start]), redirect_stderr(io.StringIO()), \
                    patch.object(RUNNER, "run") as run:
                with self.assertRaises(SystemExit):
                    RUNNER.main(incomplete)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
