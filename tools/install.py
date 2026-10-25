#!/usr/bin/env python3
"""Install the offline display UI on a Pico 2 running CircuitPython 9.2.8."""

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import string
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_NAME = "adafruit-circuitpython-bundle-9.x-mpy-20261023.zip"
BUNDLE_SHA256 = "123093d6c6ad3d7553ff9b4566d04cce0ea150fda41cfa41591efacb35ba889d"
BUNDLE_URL = "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases/download/20261023/" + BUNDLE_NAME
BUNDLE_PREFIX = BUNDLE_NAME[:-4] + "/"
PACKAGES = ("adafruit_seesaw", "adafruit_display_text", "adafruit_bitmap_font", "adafruit_bus_device")
MODULES = ("adafruit_st7789.mpy", "adafruit_ticks.mpy")
UI_MODULES = ("__init__", "app", "automation", "demo", "hardware", "inputs",
              "menu", "model", "panel", "render", "settings", "status_led")


class InstallError(RuntimeError):
    pass


class InstallCancelled(InstallError):
    pass


def discover_drives(roots=None, windows=None):
    """Inspect only standard mount locations, up to two directory levels deep."""
    windows = os.name == "nt" if windows is None else windows
    if roots is None and windows:
        return [p for letter in string.ascii_uppercase
                if (p := Path(letter + ":/")).joinpath("boot_out.txt").is_file()]
    roots = [Path(p) for p in (roots if roots is not None else
                              ("/Volumes",) if sys.platform == "darwin" else
                              ("/run/media", "/media", "/mnt"))]
    found = set()
    for root in roots:
        level = [root]
        for _ in range(3):
            following = []
            for path in level:
                if path.is_symlink() or not path.is_dir():
                    continue
                if path.name.upper() == "CIRCUITPY":
                    found.add(path.resolve())
                    continue
                try:
                    following.extend(path.iterdir())
                except OSError:
                    pass
            level = following
    return sorted(found)


def select_drive(explicit=None, candidates=None):
    candidates = [Path(explicit)] if explicit is not None else (
        discover_drives() if candidates is None else candidates)
    if len(candidates) != 1:
        raise InstallError("Expected exactly one CIRCUITPY drive; found %d. Use --drive PATH." % len(candidates))
    path = Path(candidates[0]).expanduser().absolute()
    if path.is_symlink() or not path.is_dir():
        raise InstallError("Drive must be a real directory, not a symlink: " + str(path))
    return path.resolve()


def safe_target(base, relative):
    """Reject symlinks and file/directory collisions along a relative path."""
    relative = PurePosixPath(relative)
    if relative.is_absolute() or not relative.parts or any(p in ("..", ".") for p in relative.parts):
        raise InstallError("Unsafe relative path: " + str(relative))
    current = base
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise InstallError("Symlink refused: " + str(current))
        if current.exists() and index < len(relative.parts) - 1 and not current.is_dir():
            raise InstallError("Expected directory: " + str(current))
    return current


def identify_board(drive):
    try:
        text = safe_target(drive, "boot_out.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError("Cannot read CircuitPython boot_out.txt: " + str(error)) from error
    if (not re.search(r"^Adafruit CircuitPython 9\.2\.8 on ", text, re.M)
            or not re.search(r"^Board ID:raspberry_pi_pico2\s*$", text, re.M)):
        raise InstallError("Requires Raspberry Pi Pico 2 / CircuitPython 9.2.8 exactly; other boards/versions are unvalidated.")
    uid = re.search(r"^UID:([0-9A-Fa-f]{8,64})\s*$", text, re.M)
    if uid is None:
        raise InstallError("Missing or unrecognized board UID in boot_out.txt")
    return uid.group(1).upper()


def local_files(root):
    """Read the tracked UI sources/fonts before any device mutation."""
    files = {}
    ui = safe_target(root, "Software/grinder_ui")
    paths = list(ui.glob("*.py"))
    missing = {name + ".py" for name in UI_MODULES} - {p.name for p in paths}
    if missing:
        raise InstallError("Checkout is missing required grinder_ui sources: " + ", ".join(sorted(missing)))
    for path in paths:
        source = safe_target(root, path.relative_to(root).as_posix())
        files["lib/grinder_ui/" + path.name] = source.read_bytes()
    for name in ("B612-Regular-12.bdf", "B612-Regular-14.bdf",
                 "B612Mono-Regular-36.bdf", "B612Mono-Regular-52.bdf", "OFL.txt"):
        files["fonts/" + name] = safe_target(root, "Software/assets/fonts/" + name).read_bytes()
    licenses = safe_target(root, "Software/assets/licenses")
    for component in ("Seesaw", "ST7789", "Bitmap_Font", "BusDevice", "Display_Text", "Ticks"):
        if not (licenses / ("Adafruit_CircuitPython_" + component + "-LICENSE.txt")).is_file():
            raise InstallError("Missing Adafruit license: " + component)
    for path in licenses.rglob("*"):
        relative = path.relative_to(licenses).as_posix()
        source = safe_target(root, path.relative_to(root).as_posix())
        if source.is_file():
            files["licenses/" + relative] = source.read_bytes()
    files["licenses/CoffeeGrinder-GPL-3.0.txt"] = safe_target(root, "LICENSE").read_bytes()
    files["code.py"] = safe_target(root, "Software/display_code.py").read_bytes()
    return files


def bundle_files(path, expected_hash=BUNDLE_SHA256):
    if path.is_symlink() or not path.is_file():
        raise InstallError("Bundle must be a regular local ZIP file")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise InstallError("Bundle SHA256 mismatch; expected " + expected_hash)
    files, seen = {}, set()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if ("\\" in name or name.startswith("/") or ".." in parts or ":" in name
                    or stat.S_ISLNK(info.external_attr >> 16) or name.casefold() in seen):
                raise InstallError("Unsafe/duplicate ZIP member: " + name)
            seen.add(name.casefold())
            if info.is_dir() or not name.startswith(BUNDLE_PREFIX + "lib/"):
                continue
            relative = name[len(BUNDLE_PREFIX + "lib/"):]
            if relative in MODULES or relative.split("/")[0] in PACKAGES:
                if info.file_size > 2_000_000:
                    raise InstallError("Oversized library member: " + name)
                files["lib/" + relative] = archive.read(info)
        for module in MODULES:
            if "lib/" + module not in files:
                raise InstallError("Required bundle module missing: " + module)
        for package in PACKAGES:
            if not any("lib/" + package + "/__init__." + ext in files for ext in ("py", "mpy")):
                raise InstallError("Required bundle package missing: " + package)
    return files


def acquire_bundle(root, explicit=None, dry_run=False):
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    cached = safe_target(root, ".local/install-cache/" + BUNDLE_NAME)
    if cached.is_file():
        return cached
    if dry_run:
        return None
    if cached.exists():
        raise InstallError("Bundle cache path is not a regular file: " + str(cached))
    cached.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        print("Downloading pinned Adafruit libraries: " + BUNDLE_URL)
        with urlopen(Request(BUNDLE_URL, headers={"User-Agent": "CoffeeGrinder-installer"}), timeout=30) as response:
            with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as stream:
                temporary = Path(stream.name)
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64_000_000:
                        raise InstallError("Bundle download exceeds size limit")
                    stream.write(chunk)
        bundle_files(temporary)
        os.replace(temporary, cached)
        return cached
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def preflight(drive, files):
    if not os.access(drive, os.W_OK):
        raise InstallError("CIRCUITPY is not writable")
    for relative in files:
        target = safe_target(drive, relative)
        if target.exists() and not target.is_file():
            raise InstallError("Expected file: " + str(target))
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK) or target.exists() and not os.access(target, os.W_OK):
            raise InstallError("Destination is not writable: " + str(target))
    # New bytes plus a temporary copy and FAT directory/allocation overhead.
    required = sum(len(data) for data in files.values()) + max(map(len, files.values())) + 128 * 1024
    if shutil.disk_usage(drive).free < required:
        raise InstallError("Not enough free space on CIRCUITPY (need %d bytes)" % required)


def atomic_write(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".grinder-install-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if target.read_bytes() != data:
            raise InstallError("Readback mismatch: " + str(target))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def deploy(root, drive, files, uid, sync=None):
    """Back up first; install code.py last; attempt rollback on any failure."""
    preflight(drive, files)
    originals = {name: (drive / name).read_bytes() if (drive / name).exists() else None for name in files}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = safe_target(root, ".local/backups/" + stamp + "/" + uid)
    if shutil.disk_usage(root).free < sum(len(data) for data in originals.values() if data is not None) + 128 * 1024:
        raise InstallError("Not enough local space for backup")
    backup.mkdir(parents=True, exist_ok=False)
    for name, data in originals.items():
        if data is not None:
            atomic_write(backup / name, data)
    atomic_write(backup / "RESTORE.txt", ("Original files are stored at matching relative paths.\n"
                 "Newly installed paths (absent before this install):\n" + "\n".join(
                     name for name, data in originals.items() if data is None) + "\n").encode())
    written = []
    try:
        for name in sorted(files, key=lambda name: (name == "code.py", name)):
            target = safe_target(drive, name)
            written.append(name)
            atomic_write(target, files[name])
        (sync if sync is not None else getattr(os, "sync", lambda: None))()
    except (Exception, KeyboardInterrupt) as error:
        failures = []
        for name in reversed(written):
            try:
                if originals[name] is None:
                    (drive / name).unlink(missing_ok=True)
                else:
                    atomic_write(drive / name, originals[name])
            except Exception as restore_error:
                failures.append(str(restore_error))
        cancelled = isinstance(error, KeyboardInterrupt)
        failure = InstallCancelled if cancelled else InstallError
        reason = "Install cancelled by Ctrl-C" if cancelled else "Install failed: " + str(error)
        raise failure("%s. Backup: %s. %s" %
                      (reason, backup, "Rollback incomplete: " + "; ".join(failures) if failures else "Original files restored.")) from error
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive", type=Path, help="CIRCUITPY mount path (otherwise detected)")
    parser.add_argument("--bundle", type=Path, help="pinned Adafruit ZIP for offline installation")
    parser.add_argument("--dry-run", action="store_true", help="validate/plan without downloads or writes")
    args = parser.parse_args(argv)
    try:
        drive = select_drive(args.drive)
        uid = identify_board(drive)
        files = local_files(ROOT)
        bundle = acquire_bundle(ROOT, args.bundle, args.dry_run)
        if bundle is None:
            preflight(drive, files)
            print("DRY RUN: would download and verify " + BUNDLE_URL)
            print("SHA256: " + BUNDLE_SHA256)
            print("Then install required UI libraries, fonts and code.py last on " + str(drive))
            return 0
        files.update(bundle_files(bundle))
        preflight(drive, files)
        print("Pico 2 / CircuitPython 9.2.8, UID %s: %d files, %d bytes" %
              (uid, len(files), sum(map(len, files.values()))))
        if args.dry_run:
            print("DRY RUN: no changes; code.py would be installed last. Drive: " + str(drive))
        else:
            backup = deploy(ROOT, drive, files, uid)
            print("Installed offline UI. Backup: " + str(backup))
            print("Safely eject CIRCUITPY, then reset the Pico if the UI has not started.")
        return 0
    except InstallCancelled as error:
        print(str(error), file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("Install cancelled by Ctrl-C before device installation.", file=sys.stderr)
        return 130
    except (InstallError, OSError, zipfile.BadZipFile) as error:
        print("Install error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
