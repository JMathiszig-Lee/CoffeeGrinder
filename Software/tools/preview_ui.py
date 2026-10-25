"""Render the shared LCD layout to PNGs with the original, licensed B612 fonts.

Usage: python Software/tools/preview_ui.py
Pillow is needed only on the laptop; the device uses the generated BDF assets.
"""

from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Software"))
from grinder_ui import panel


def render(view, source_directory=None):
    sources = source_directory or ROOT / ".local/ui-sources"
    def rgb(colour):
        return (colour >> 16 & 255, colour >> 8 & 255, colour & 255)
    image = Image.new("RGB", (panel.WIDTH, panel.HEIGHT), rgb(panel.BG))
    draw = ImageDraw.Draw(image)
    fonts = {}

    def text(value, font_name, x, y, ax, ay, colour):
        if font_name not in fonts:
            family, size = font_name.rsplit("-", 1)
            fonts[font_name] = ImageFont.truetype(str(sources / (family + ".ttf")), int(size))
        font = fonts[font_name]
        value = value.upper()
        if y == 220:
            while font.getlength(value) > 304:
                value = value[:-1]
        left, top, right, bottom = font.getbbox(value)
        width, height = right - left, bottom - top
        mask = Image.new("1", (max(1, width), max(1, height)))
        ImageDraw.Draw(mask).text((-left, -top), value, font=font, fill=1)
        image.paste(rgb(colour), (round(x - width * ax), round(y - height * ay)), mask)

    formatted = panel.presentation(view)
    if view.get("menu"):
        for side in (0, 315):
            for y in (60, 180):
                draw.line((side, y, side + 3, y), fill=rgb(panel.WHITE))
        draw.rectangle((panel.BAR_X, panel.BAR_Y, panel.BAR_X + panel.BAR_W - 1,
                        panel.BAR_Y + panel.BAR_H - 1), outline=rgb(panel.LINE))
        if formatted["load_width"]:
            draw.rectangle((panel.BAR_X + 2, panel.BAR_Y + 2,
                            panel.BAR_X + 1 + formatted["load_width"], panel.BAR_Y + panel.BAR_H - 3),
                           fill=rgb(formatted["load_colour"]))
        for _name, value, font, x, y, ax, ay, colour in panel.MENU_STATIC_TEXT:
            text(value, font, x, y, ax, ay, colour)
        menu = panel.menu_presentation(view)
        for name, font, x, y, ax, ay in panel.MENU_DYNAMIC_TEXT:
            value, colour = menu[name]
            text(value, font, x, y, ax, ay, colour)
        return image
    if formatted["actual_sector"] is not None:
        draw.polygon(formatted["actual_sector"], fill=rgb(panel.FILL))
    x, y, width, height = panel.READOUT
    draw.rectangle((x, y, x + width - 1, y + height - 1), fill=rgb(panel.BG))
    for x1, y1, x2, y2, colour in panel.static_lines():
        draw.line((x1, y1, x2, y2), fill=rgb(colour))
    draw.rectangle((x, y, x + width - 1, y + height - 1), outline=rgb(panel.WHITE))
    draw.rectangle((panel.BAR_X, panel.BAR_Y, panel.BAR_X + panel.BAR_W - 1,
                    panel.BAR_Y + panel.BAR_H - 1), outline=rgb(panel.LINE))
    middle = panel.BAR_X + panel.BAR_W // 2
    draw.line((middle, panel.BAR_Y + 1, middle, panel.BAR_Y + panel.BAR_H - 2), fill=rgb(panel.LINE))
    caution = panel.BAR_X + 2 + round((panel.BAR_W - 4) * .8)
    draw.line((caution, panel.BAR_Y - 2, caution, panel.BAR_Y + panel.BAR_H + 1), fill=rgb(panel.AMBER))
    if formatted["actual_pointer"] is not None:
        draw.polygon(formatted["actual_pointer"], fill=rgb(panel.WHITE))
    draw.polygon(formatted["selected_pointer"], fill=rgb(panel.CYAN))
    if formatted["load_width"]:
        draw.rectangle((panel.BAR_X + 2, panel.BAR_Y + 2,
                        panel.BAR_X + 1 + formatted["load_width"], panel.BAR_Y + panel.BAR_H - 3),
                       fill=rgb(formatted["load_colour"]))
    for _name, value, font, x, y, ax, ay, colour in panel.STATIC_TEXT:
        text(value, font, x, y, ax, ay, colour)
    for name, font, x, y, ax, ay in panel.DYNAMIC_TEXT:
        value, colour = formatted[name]
        text(value, font, x, y, ax, ay, colour)
    return image


def main():
    destination = ROOT / "Software/previews"
    destination.mkdir(exist_ok=True)
    base = {"mode": "offline", "state": "offline", "set_rpm": 110, "default_rpm": 110,
            "actual_rpm": None, "load_percent": None, "fault": None, "input_error": None,
            "notice": None}
    cases = {
        "offline": base,
        "simulation": dict(base, mode="simulation", state="running", set_rpm=100,
                           actual_rpm=99.4, load_percent=48),
        "high_load": dict(base, mode="simulation", state="running", actual_rpm=96, load_percent=88),
        "fault": dict(base, mode="live", state="fault", fault="Motor telemetry stale"),
    }
    sheet = Image.new("RGB", (640, 480), (8, 12, 16))
    for i, (name, view) in enumerate(cases.items()):
        preview = render(view)
        preview.save(destination / (name + ".png"))
        sheet.paste(preview, ((i % 2) * 320, (i // 2) * 240))
    sheet.resize((1280, 960), Image.Resampling.NEAREST).save(destination / "instrument_preview.png")
    from grinder_ui.model import UiModel
    from grinder_ui.settings import NvmSettings
    ui = UiModel(settings=NvmSettings(bytearray(64)))
    menu_sheet = Image.new("RGB", (640, 480), (8, 12, 16))
    menu_sheet.paste(render(cases["simulation"]), (0, 0))
    ui.menu.open()
    for index, name in enumerate(("settings", "autostop", "recovery"), 1):
        ui.menu.page = name
        ui.menu.cursor = 2 if name == "settings" else 0
        preview = render(ui.view())
        preview.save(destination / ("menu_" + name + ".png"))
        menu_sheet.paste(preview, ((index % 2) * 320, (index // 2) * 240))
    menu_sheet.resize((1280, 960), Image.Resampling.NEAREST).save(destination / "menu_preview.png")
    print(destination / "instrument_preview.png")
    print(destination / "menu_preview.png")


if __name__ == "__main__":
    main()
