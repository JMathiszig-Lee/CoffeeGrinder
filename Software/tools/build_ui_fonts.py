#!/usr/bin/env python3
"""Build the UI's one-bit BDF fonts from the pinned official B612 TTF files.

Host dependency: Pillow. No font tools or CircuitPython hardware are required.
Run from any directory; default paths are relative to this repository.
"""

import argparse
import hashlib
import math
from pathlib import Path
import re
import shutil

import PIL
from PIL import Image, ImageDraw, ImageFont, features


REPOSITORY = Path(__file__).resolve().parents[2]
ASCII = "".join(chr(code) for code in range(32, 127))
# Adafruit Display_Text's LabelBase uses capital M to measure character size.
NUMERIC = " -.0123456789M"
SPECS = (
    ("B612-Regular.ttf", 12, ASCII),
    ("B612-Regular.ttf", 14, ASCII),
    ("B612Mono-Regular.ttf", 52, NUMERIC),
    ("B612Mono-Regular.ttf", 36, NUMERIC),
)
THRESHOLD = 128


def nearest(value):
    """Round positive pixel metrics consistently, including exact half pixels."""
    return int(math.floor(value + 0.5))


def rasterize(font, character):
    """Return BDF bounds, advance and top-to-bottom, MSB-first bitmap rows."""
    left, top, right, bottom = font.getbbox(character, anchor="ls")
    width, height = right - left, bottom - top
    advance = nearest(font.getlength(character))
    if width <= 0 or height <= 0:
        # Adafruit's loader constructs a displayio.Bitmap for every glyph.
        # Give whitespace a nonzero blank bitmap, retaining its real advance.
        left, top, right, bottom = 0, -1, 1, 0
        width = height = 1
        mask = Image.new("L", (width, height), 0)
    else:
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text(
            (-left, -top), character, font=font, anchor="ls", fill=255
        )
    row_bytes = (width + 7) // 8
    rows = []
    for y in range(height):
        row = bytearray(row_bytes)
        for x in range(width):
            if mask.getpixel((x, y)) >= THRESHOLD:
                row[x // 8] |= 1 << (7 - x % 8)
        rows.append(row.hex().upper())
    return {
        "character": character,
        "bounds": (left, top, right, bottom),
        "advance": advance,
        "scalable_width": nearest(font.getlength(character) * 1000 / font.size),
        "rows": rows,
    }


def build_font(source, size, characters, revision):
    font = ImageFont.truetype(str(source), size=size)
    glyphs = [rasterize(font, char) for char in sorted(set(characters), key=ord)]
    left = min(glyph["bounds"][0] for glyph in glyphs)
    top = min(glyph["bounds"][1] for glyph in glyphs)
    right = max(glyph["bounds"][2] for glyph in glyphs)
    bottom = max(glyph["bounds"][3] for glyph in glyphs)
    ascent, descent = font.getmetrics()
    mono = source.stem.startswith("B612Mono")
    family, spacing = ("B612Mono", "c") if mono else ("B612", "p")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    default = ord("?") if "?" in characters else ord("-")
    lines = [
        "STARTFONT 2.1",
        "COMMENT One-bit B612 conversion for CoffeeGrinder; SIL OFL 1.1, see OFL.txt",
        "COMMENT Source https://github.com/polarsys/b612/tree/" + revision,
        "COMMENT Source TTF SHA256 " + digest,
        "COMMENT Pillow %s; FreeType %s; threshold %d; baseline metrics"
        % (PIL.__version__, features.version_module("freetype2"), THRESHOLD),
        "FONT -CoffeeGrinder-%s-medium-r-normal--%d-%d-72-72-%s-0-iso10646-1"
        % (family, size, size * 10, spacing),
        "SIZE %d 72 72" % size,
        "FONTBOUNDINGBOX %d %d %d %d" % (right - left, bottom - top, left, -bottom),
        "STARTPROPERTIES 6",
        "FONT_ASCENT %d" % ascent,
        "FONT_DESCENT %d" % descent,
        "DEFAULT_CHAR %d" % default,
        'FAMILY_NAME "%s"' % family,
        'COPYRIGHT "Copyright 2012 The B612 Project Authors; SIL OFL 1.1"',
        'FONT_VERSION "B612 %s; Pillow bitmap conversion"' % revision,
        "ENDPROPERTIES",
        "CHARS %d" % len(glyphs),
    ]
    for glyph in glyphs:
        code = ord(glyph["character"])
        x0, y0, x1, y1 = glyph["bounds"]
        lines.extend((
            "STARTCHAR uni%04X" % code,
            "ENCODING %d" % code,
            "SWIDTH %d 0" % glyph["scalable_width"],
            "DWIDTH %d 0" % glyph["advance"],
            "BBX %d %d %d %d" % (x1 - x0, y1 - y0, x0, -y1),
            "BITMAP",
        ))
        lines.extend(glyph["rows"])
        lines.append("ENDCHAR")
    lines.append("ENDFONT")
    return "\n".join(lines) + "\n", len(glyphs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path,
                        default=REPOSITORY / ".local/ui-sources")
    parser.add_argument("--output-dir", type=Path,
                        default=REPOSITORY / "Software/assets/fonts")
    args = parser.parse_args(argv)
    revision = (args.source_dir / "b612-revision.txt").read_text().strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        parser.error("b612-revision.txt must contain the pinned 40-character Git SHA")
    license_source = args.source_dir / "OFL.txt"
    if b"SIL OPEN FONT LICENSE Version 1.1" not in license_source.read_bytes():
        parser.error("The source directory must include B612's complete OFL.txt")
    # Validate all inputs before writing any output.
    sources = {name: args.source_dir / name for name, _, _ in SPECS}
    for source in sources.values():
        if not source.is_file():
            parser.error("Missing source font: " + str(source))
    output = []
    for name, size, characters in SPECS:
        content, count = build_font(sources[name], size, characters, revision)
        output.append(("%s-%d.bdf" % (Path(name).stem, size), content, count))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(license_source, args.output_dir / "OFL.txt")
    for name, content, count in output:
        destination = args.output_dir / name
        destination.write_text(content, encoding="ascii", newline="\n")
        print("%s: %d glyphs, %d bytes" % (destination.name, count, len(content)))


if __name__ == "__main__":
    main()
