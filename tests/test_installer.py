"""Installer checks use temporary files only; never discover real USB volumes."""

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


SOURCE = Path(__file__).resolve().parents[1] / "tools/install.py"
spec = importlib.util.spec_from_file_location("grinder_installer", SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
BOOT = ("Adafruit CircuitPython 9.2.8 on 2025-05-28; Raspberry Pi Pico 2 with rp2350a\n"
        "Board ID:raspberry_pi_pico2\nUID:0011223344556677\n")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir()
        self.drive = Path(self.temporary.name) / "CIRCUITPY"
        self.drive.mkdir()
        (self.drive / "boot_out.txt").write_text(BOOT)

    def source_tree(self):
        (self.root / "LICENSE").write_text("Project GPLv3 license\n")
        for name in (module + ".py" for module in installer.UI_MODULES):
            path = self.root / "Software/grinder_ui" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# UI source\n")
        fonts = self.root / "Software/assets/fonts"
        fonts.mkdir(parents=True)
        for name in ("B612-Regular-12.bdf", "B612-Regular-14.bdf",
                     "B612Mono-Regular-36.bdf", "B612Mono-Regular-52.bdf", "OFL.txt"):
            (fonts / name).write_text("font or complete license\n")
        licenses = self.root / "Software/assets/licenses"
        licenses.mkdir()
        for name in ("Seesaw", "ST7789", "Bitmap_Font", "BusDevice", "Display_Text", "Ticks"):
            (licenses / ("Adafruit_CircuitPython_" + name + "-LICENSE.txt")).write_text("MIT license\n")
        (licenses / "README.md").write_text("License provenance\n")
        (self.root / "Software/display_code.py").write_text("from grinder_ui.app import main\nmain()\n")

    def bundle(self, extra=None, omit=None):
        path = Path(self.temporary.name) / "bundle.zip"
        entries = {installer.BUNDLE_PREFIX + "lib/" + name: b"compiled module" for name in installer.MODULES}
        for package in installer.PACKAGES:
            entries[installer.BUNDLE_PREFIX + "lib/" + package + "/__init__.mpy"] = b"package"
        entries[installer.BUNDLE_PREFIX + "lib/not_needed.mpy"] = b"unrelated"
        if omit:
            entries.pop(installer.BUNDLE_PREFIX + "lib/" + omit)
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            if extra is not None:
                archive.writestr(extra, b"malicious")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_detects_nested_mounts_and_refuses_ambiguity(self):
        mounts = Path(self.temporary.name) / "mounts"
        one, two = mounts / "alice/CIRCUITPY", mounts / "bob/CIRCUITPY"
        one.mkdir(parents=True)
        two.mkdir(parents=True)
        found = installer.discover_drives([mounts])
        self.assertEqual(found, sorted((one, two)))
        with self.assertRaisesRegex(installer.InstallError, "exactly one"):
            installer.select_drive(candidates=found)
        self.assertEqual(installer.select_drive(one), one)
        with self.assertRaises(installer.InstallError):
            installer.select_drive(candidates=[])

    def test_exact_firmware_board_and_uid_are_required(self):
        self.assertEqual(installer.identify_board(self.drive), "0011223344556677")
        for text in (BOOT.replace("9.2.8", "9.2.7"), BOOT.replace("9.2.8", "10.0.0"),
                     BOOT.replace("Board ID:raspberry_pi_pico2", "Board ID:raspberry_pi_pico2_w"),
                     BOOT.replace("UID:0011223344556677", "UID:../escape")):
            (self.drive / "boot_out.txt").write_text(text)
            with self.assertRaises(installer.InstallError):
                installer.identify_board(self.drive)

    def test_local_plan_contains_only_ui_fonts_licenses_and_startup(self):
        self.source_tree()
        files = installer.local_files(self.root)
        self.assertIn("fonts/OFL.txt", files)
        self.assertIn("licenses/Adafruit_CircuitPython_Seesaw-LICENSE.txt", files)
        self.assertIn("licenses/README.md", files)
        self.assertIn("licenses/CoffeeGrinder-GPL-3.0.txt", files)
        self.assertIn("code.py", files)
        self.assertTrue(all(name.startswith(("lib/grinder_ui/", "fonts/", "licenses/"))
                            or name == "code.py" for name in files))

    def test_missing_ui_module_fails_before_bundle_backup_or_device_writes(self):
        self.source_tree()
        before = {p.name: p.read_bytes() for p in self.drive.iterdir()}
        for module in ("model", "menu", "automation", "demo", "render", "inputs", "panel", "status_led"):
            path = self.root / "Software/grinder_ui" / (module + ".py")
            content = path.read_bytes()
            path.unlink()
            with self.subTest(module=module), patch.object(installer, "ROOT", self.root):
                with patch.object(installer, "acquire_bundle") as acquire:
                    with contextlib.redirect_stderr(io.StringIO()) as output:
                        result = installer.main(["--drive", str(self.drive)])
                self.assertEqual(result, 1)
                self.assertIn(module + ".py", output.getvalue())
                acquire.assert_not_called()
                self.assertFalse((self.root / ".local").exists())
                self.assertEqual({p.name: p.read_bytes() for p in self.drive.iterdir()}, before)
            path.write_bytes(content)

    def test_bundle_hash_required_components_and_selective_contents(self):
        path, digest = self.bundle()
        files = installer.bundle_files(path, digest)
        self.assertEqual(len(files), 6)
        self.assertNotIn("lib/not_needed.mpy", files)
        with self.assertRaisesRegex(installer.InstallError, "SHA256"):
            installer.bundle_files(path, "0" * 64)
        path, digest = self.bundle(omit="adafruit_ticks.mpy")
        with self.assertRaisesRegex(installer.InstallError, "missing"):
            installer.bundle_files(path, digest)

    def test_traversal_and_symlink_zip_members_are_rejected_before_extraction(self):
        for member in ("../escape", "/absolute", "folder\\escape", "C:/escape"):
            path, digest = self.bundle(extra=member)
            with self.subTest(member=member), self.assertRaisesRegex(installer.InstallError, "Unsafe"):
                installer.bundle_files(path, digest)
        link = zipfile.ZipInfo(installer.BUNDLE_PREFIX + "lib/adafruit_seesaw/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        path, digest = self.bundle(extra=link)
        with self.assertRaisesRegex(installer.InstallError, "Unsafe"):
            installer.bundle_files(path, digest)
        self.assertFalse((Path(self.temporary.name) / "escape").exists())

    def test_symlink_destination_and_source_are_refused(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        try:
            (self.drive / "lib").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaisesRegex(installer.InstallError, "Symlink"):
            installer.preflight(self.drive, {"lib/app.py": b"new", "code.py": b"new"})
        self.assertEqual(list(outside.iterdir()), [])
        self.source_tree()
        source = self.root / "Software/grinder_ui/app.py"
        source.unlink()
        source.symlink_to(self.drive / "boot_out.txt")
        with self.assertRaisesRegex(installer.InstallError, "Symlink"):
            installer.local_files(self.root)

    def test_insufficient_space_does_not_create_device_files(self):
        before = sorted(self.drive.rglob("*"))
        with patch.object(installer.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(installer.InstallError, "free space"):
                installer.preflight(self.drive, {"code.py": b"startup"})
        self.assertEqual(sorted(self.drive.rglob("*")), before)

    def test_successful_install_backs_up_verifies_and_writes_startup_last(self):
        (self.drive / "code.py").write_bytes(b"original startup")
        (self.drive / "keep.txt").write_bytes(b"unrelated")
        files = {"code.py": b"offline startup", "lib/grinder_ui/app.py": b"UI",
                 "fonts/OFL.txt": b"font license"}
        writes, syncs = [], []
        original = installer.atomic_write

        def record(target, data):
            try:
                writes.append(target.relative_to(self.drive).as_posix())
            except ValueError:
                pass
            original(target, data)

        with patch.object(installer, "atomic_write", side_effect=record):
            backup = installer.deploy(self.root, self.drive, files, "ABCDEF0123456789", lambda: syncs.append(True))
        self.assertEqual(writes[-1], "code.py")
        self.assertEqual((backup / "code.py").read_bytes(), b"original startup")
        self.assertEqual((self.drive / "keep.txt").read_bytes(), b"unrelated")
        for name, data in files.items():
            self.assertEqual((self.drive / name).read_bytes(), data)
        self.assertEqual(syncs, [True])
        self.assertFalse(list(self.drive.rglob(".grinder-install-*")))

    def test_partial_failure_rolls_back_and_never_replaces_startup(self):
        (self.drive / "code.py").write_bytes(b"old startup")
        target = self.drive / "lib/grinder_ui/app.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old UI")
        original = installer.atomic_write
        startup_writes = []

        def fail_one(target, data):
            if target == self.drive / "code.py":
                startup_writes.append(data)
            if target == self.drive / "lib/grinder_ui/app.py" and data == b"new UI":
                raise OSError("Simulated copy failure")
            original(target, data)

        files = {"code.py": b"new startup", "fonts/OFL.txt": b"new font license",
                 "lib/grinder_ui/app.py": b"new UI"}
        with patch.object(installer, "atomic_write", side_effect=fail_one):
            with self.assertRaisesRegex(installer.InstallError, "Original files restored"):
                installer.deploy(self.root, self.drive, files, "ABCDEF0123456789", lambda: None)
        self.assertEqual(startup_writes, [])
        self.assertEqual((self.drive / "code.py").read_bytes(), b"old startup")
        self.assertEqual(target.read_bytes(), b"old UI")
        self.assertFalse((self.drive / "fonts/OFL.txt").exists())
        backups = list((self.root / ".local/backups").glob("*/ABCDEF0123456789/code.py"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old startup")

    def test_interrupted_copy_rolls_back_before_startup_is_replaced(self):
        (self.drive / "code.py").write_bytes(b"old startup")
        target = self.drive / "lib/grinder_ui/app.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old UI")
        original = installer.atomic_write
        startup_writes = []

        def interrupt_one(path, data):
            if path == self.drive / "code.py":
                startup_writes.append(data)
            original(path, data)
            if path == target and data == b"new UI":
                # Interrupt after replacement, when rollback must restore it.
                raise KeyboardInterrupt

        files = {"code.py": b"new startup", "lib/grinder_ui/app.py": b"new UI"}
        with patch.object(installer, "atomic_write", side_effect=interrupt_one):
            with self.assertRaisesRegex(installer.InstallCancelled, "Ctrl-C.*Backup:.*Original files restored"):
                installer.deploy(self.root, self.drive, files, "ABCDEF0123456789", lambda: None)
        self.assertEqual(startup_writes, [])
        self.assertEqual((self.drive / "code.py").read_bytes(), b"old startup")
        self.assertEqual(target.read_bytes(), b"old UI")

    def test_dry_run_without_cache_does_not_download_or_write_anywhere(self):
        self.source_tree()
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        with patch.object(installer, "ROOT", self.root), patch.object(installer, "urlopen") as request:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = installer.main(["--drive", str(self.drive), "--dry-run"])
        self.assertEqual(result, 0)
        self.assertIn("would download and verify", output.getvalue())
        request.assert_not_called()
        after = {p.relative_to(self.root).as_posix(): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / ".local").exists())
        self.assertEqual(list(self.drive.iterdir()), [self.drive / "boot_out.txt"])


if __name__ == "__main__":
    unittest.main()
