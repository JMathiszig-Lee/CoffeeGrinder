# B612 bitmap fonts

These one-bit BDF files are converted from the official
[Eclipse/PolarSys B612 fonts](https://github.com/polarsys/b612), pinned to
commit `48ac6ba67ecab8123e8e36d6aa05367db0c7b638`. They are modified font
assets distributed under **SIL Open Font License 1.1**. Keep the complete
[OFL.txt](OFL.txt), including the original copyright notice, with copies of
these assets. No Reserved Font Names are listed in that licence.

| Asset | Included characters | Pixel advance / global bounding box |
| --- | --- | --- |
| `B612-Regular-12.bdf` | Printable ASCII 32–126 | Proportional; 13×14 box, offset −1,−3 |
| `B612-Regular-14.bdf` | Printable ASCII 32–126 | Proportional; 15×17 box, offset −1,−4 |
| `B612Mono-Regular-52.bdf` | Space, digits, `-`, `.`, `M` | 34 px advance; 34×40 box |
| `B612Mono-Regular-36.bdf` | Space, digits, `-`, `.`, `M` | 23 px advance; 23×27 box |

The filename size is the FreeType pixel em size, not the visible capital
height. Use the generated glyph metrics to position text. The numeric fonts
include capital `M` because
[Adafruit Display_Text LabelBase](https://docs.circuitpython.org/projects/display_text/en/latest/api.html)
uses it to measure character size. Other letters and `%` are omitted; draw
units, percent signs and labels with a Regular font.

## Rebuild

Use Python with Pillow; the committed files were generated with **Pillow
12.3.0 / FreeType 2.14.3**. Different rasterizer versions can change pixels.
No `otf2bdf`, `bdftopcf`, network access or Pico connection is needed to run
the converter once its inputs are present.

Place these pinned official downloads in `.local/ui-sources/` at the project
root, retaining their filenames:

- [B612-Regular.ttf](https://raw.githubusercontent.com/polarsys/b612/48ac6ba67ecab8123e8e36d6aa05367db0c7b638/fonts/ttf/B612-Regular.ttf)
- [B612Mono-Regular.ttf](https://raw.githubusercontent.com/polarsys/b612/48ac6ba67ecab8123e8e36d6aa05367db0c7b638/fonts/ttf/B612Mono-Regular.ttf)
- [OFL.txt](https://raw.githubusercontent.com/polarsys/b612/48ac6ba67ecab8123e8e36d6aa05367db0c7b638/OFL.txt)

Also create `.local/ui-sources/b612-revision.txt` containing the pinned commit
ID above. From the project root:

```bash
python Software/tools/build_ui_fonts.py
```

Use `--source-dir` and `--output-dir` to supply different local paths. The
builder validates the inputs before writing output and copies the source
licence unchanged. Each BDF records the revision, TTF SHA-256 and rasterizer
versions; generated output contains no timestamps.

## Format and loading

The converter renders grayscale glyphs with Pillow, then thresholds at 128
into one-bit rows. It uses baseline-relative bounds from
`font.getbbox(character, anchor="ls")`, maps BDF offsets to `x=left,
y=-bottom`, and rounds each glyph's advance from `font.getlength()`.
Ascender/descender metrics are retained. Rows are top to bottom, padded to
whole bytes and encoded most-significant pixel first. Space has a blank 1×1
bitmap so loaders need not allocate zero-height bitmaps; its advance is
preserved.

Copy the BDF files and licence to the application's font directory. With
[Adafruit bitmap-font](https://docs.circuitpython.org/projects/bitmap-font/en/latest/):

```python
from adafruit_bitmap_font import bitmap_font

rpm_font = bitmap_font.load_font("/fonts/B612Mono-Regular-52.bdf")
rpm_font.load_glyphs(" -.0123456789M")
```

Preload needed glyphs before the control/display loop. The generated files
were checked with Adafruit's BDF parser on a host bitmap stub: every included
glyph loaded, spaces remained blank, negative bearings and descenders were
preserved, and numeric advances were uniform. This does not replace checking
readability on the physical display.
