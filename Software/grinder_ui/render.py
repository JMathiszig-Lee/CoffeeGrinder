"""Retained DisplayIO instrument; only changed text/shapes are refreshed."""

from . import panel


class InstrumentDisplay:
    def __init__(self, display, font_directory="/fonts"):
        import displayio
        import vectorio
        from adafruit_bitmap_font import bitmap_font
        from adafruit_display_text import bitmap_label as label

        self.display = display
        self.group = displayio.Group()
        self.labels = {}
        self._last = {}
        self._fonts = {}
        self._label_fonts = {}
        for name in ("B612-Regular-12", "B612-Regular-14", "B612Mono-Regular-52", "B612Mono-Regular-36"):
            font = bitmap_font.load_font(font_directory + "/" + name + ".bdf")
            font.load_glyphs("0123456789-. M" if "Mono" in name else "".join(chr(i) for i in range(32, 127)))
            self._fonts[name] = font

        bitmap = displayio.Bitmap(panel.WIDTH, panel.HEIGHT, len(panel.COLOURS))
        palette = displayio.Palette(len(panel.COLOURS))
        for i, colour in enumerate(panel.COLOURS):
            palette[i] = colour
        self._background = bitmap
        self._indices = {colour: i for i, colour in enumerate(panel.COLOURS)}
        bitmap.fill(0)
        # Transparent line work sits over the changing filled sector. Separate
        # opaque masks keep the numeric readout legible over any gauge value.
        palette.make_transparent(0)
        for x1, y1, x2, y2, colour in panel.static_lines():
            self._line(x1, y1, x2, y2, colour)
        x, y, width, height = panel.READOUT
        self._rectangle(x, y, width, height, panel.WHITE)
        self._rectangle(panel.BAR_X, panel.BAR_Y, panel.BAR_W, panel.BAR_H, panel.LINE)
        self._line(panel.BAR_X + panel.BAR_W // 2, panel.BAR_Y + 1,
                   panel.BAR_X + panel.BAR_W // 2, panel.BAR_Y + panel.BAR_H - 2, panel.LINE)
        caution = panel.BAR_X + 2 + round((panel.BAR_W - 4) * .8)
        self._line(caution, panel.BAR_Y - 2, caution, panel.BAR_Y + panel.BAR_H + 1, panel.AMBER)
        black = displayio.Palette(1)
        black[0] = panel.BG
        self.group.append(vectorio.Rectangle(pixel_shader=black, width=320, height=240))
        self._sector_group = displayio.Group()
        grey = displayio.Palette(1)
        grey[0] = panel.FILL
        self._sector_shape = vectorio.Polygon(pixel_shader=grey, points=[(0, 0), (1, 0), (0, 1)])
        self._sector_group.append(self._sector_shape)
        self._sector_group.hidden = True
        self.group.append(self._sector_group)
        x, y, width, height = panel.READOUT
        self.group.append(vectorio.Rectangle(pixel_shader=black, x=x, y=y, width=width, height=height))
        self.group.append(displayio.TileGrid(bitmap, pixel_shader=palette))

        self._actual_group = displayio.Group()
        white = displayio.Palette(1)
        white[0] = panel.WHITE
        self._actual_shape = vectorio.Polygon(pixel_shader=white, points=list(panel.pointer(0)))
        self._actual_group.append(self._actual_shape)
        self.group.append(self._actual_group)
        cyan = displayio.Palette(1)
        cyan[0] = panel.CYAN
        self._selected_shape = vectorio.Polygon(pixel_shader=cyan, points=list(panel.pointer(110, True)))
        self.group.append(self._selected_shape)

        self._bar_group = displayio.Group()
        self._bar_palette = displayio.Palette(1)
        self._bar_palette[0] = panel.WHITE
        self._bar_shape = vectorio.Rectangle(pixel_shader=self._bar_palette,
                                             width=1, height=panel.BAR_H - 4,
                                             x=panel.BAR_X + 2, y=panel.BAR_Y + 2)
        self._bar_group.append(self._bar_shape)
        self.group.append(self._bar_group)

        def add(name, text, font, x, y, ax, ay, colour):
            item = label.Label(self._fonts[font], text=text, color=colour,
                               anchor_point=(ax, ay), anchored_position=(x, y))
            self.labels[name] = item
            self._label_fonts[name] = font
            self.group.append(item)

        for spec in panel.STATIC_TEXT:
            add(*spec)
        for name, font, x, y, ax, ay in panel.DYNAMIC_TEXT:
            initial = "---" if name == "actual" else "110" if name == "selected" else " "
            add(name, initial, font, x, y, ax, ay, panel.WHITE)
        self._menu_group = displayio.Group()
        self._menu_group.append(vectorio.Rectangle(pixel_shader=black, width=320, height=240))
        for x in (0, 315):
            for y in (60, 180):
                self._menu_group.append(vectorio.Rectangle(pixel_shader=white, x=x, y=y,
                                                            width=4, height=1))
        line_palette = displayio.Palette(1)
        line_palette[0] = panel.LINE
        self._menu_group.append(vectorio.Rectangle(pixel_shader=line_palette, x=panel.BAR_X,
                                                    y=panel.BAR_Y, width=panel.BAR_W, height=panel.BAR_H))
        self._menu_group.append(vectorio.Rectangle(pixel_shader=black, x=panel.BAR_X + 1,
                                                    y=panel.BAR_Y + 1, width=panel.BAR_W - 2,
                                                    height=panel.BAR_H - 2))
        self._menu_bar_group = displayio.Group()
        self._menu_bar_palette = displayio.Palette(1)
        self._menu_bar_palette[0] = panel.WHITE
        self._menu_bar_shape = vectorio.Rectangle(pixel_shader=self._menu_bar_palette,
                                                   x=panel.BAR_X + 2, y=panel.BAR_Y + 2,
                                                   width=1, height=panel.BAR_H - 4)
        self._menu_bar_group.append(self._menu_bar_shape)
        self._menu_group.append(self._menu_bar_group)
        for spec in panel.MENU_STATIC_TEXT + [
                (name, " ", font, x, y, ax, ay, panel.WHITE)
                for name, font, x, y, ax, ay in panel.MENU_DYNAMIC_TEXT]:
            name, text, font, x, y, ax, ay, colour = spec
            item = label.Label(self._fonts[font], text=text, color=colour,
                               anchor_point=(ax, ay), anchored_position=(x, y))
            self.labels[name] = item
            self._label_fonts[name] = font
            self._menu_group.append(item)
        self._menu_visible = False
        display.root_group = self.group

    def _line(self, x1, y1, x2, y2, colour):
        index = self._indices[colour]
        dx, dy = abs(x2 - x1), -abs(y2 - y1)
        sx, sy = 1 if x1 < x2 else -1, 1 if y1 < y2 else -1
        error = dx + dy
        while True:
            if 0 <= x1 < panel.WIDTH and 0 <= y1 < panel.HEIGHT:
                self._background[x1, y1] = index
            if x1 == x2 and y1 == y2:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x1 += sx
            if twice <= dx:
                error += dx
                y1 += sy

    def _rectangle(self, x, y, width, height, colour):
        self._line(x, y, x + width - 1, y, colour)
        self._line(x, y + height - 1, x + width - 1, y + height - 1, colour)
        self._line(x, y, x, y + height - 1, colour)
        self._line(x + width - 1, y, x + width - 1, y + height - 1, colour)

    def update(self, view):
        if view.get("menu"):
            return self._update_menu(view)
        formatted = panel.presentation(view)
        changed = self._menu_visible
        if changed:
            self.display.root_group = self.group
            self._menu_visible = False
        for name, _font, _x, _y, _ax, _ay in panel.DYNAMIC_TEXT:
            value = formatted[name]
            if name == "notice":
                text, colour = value
                font = self._fonts[self._label_fonts[name]]
                while sum(font.get_glyph(ord(char)).shift_x for char in text) > 304:
                    text = text[:-1]
                value = text, colour
            if value != self._last.get(name):
                self.labels[name].text, self.labels[name].color = value
                self._last[name] = value
                changed = True
        if formatted["actual_sector"] != self._last.get("actual_sector"):
            points = formatted["actual_sector"]
            self._sector_group.hidden = points is None
            if points is not None:
                self._sector_shape.points = list(points)
            self._last["actual_sector"] = points
            changed = True
        if formatted["actual_pointer"] != self._last.get("actual_pointer"):
            points = formatted["actual_pointer"]
            self._actual_group.hidden = points is None
            if points is not None:
                self._actual_shape.points = list(points)
            self._last["actual_pointer"] = points
            changed = True
        # The first offline frame must hide the pointer too.
        self._actual_group.hidden = formatted["actual_pointer"] is None
        if formatted["selected_pointer"] != self._last.get("selected_pointer"):
            self._selected_shape.points = list(formatted["selected_pointer"])
            self._last["selected_pointer"] = formatted["selected_pointer"]
            changed = True
        width, colour = formatted["load_width"], formatted["load_colour"]
        if (width, colour) != self._last.get("bar"):
            self._bar_group.hidden = width == 0
            self._bar_shape.width = max(1, width)
            self._bar_palette[0] = colour
            self._last["bar"] = (width, colour)
            changed = True
        if changed:
            self.display.refresh(minimum_frames_per_second=0)
        return changed

    def _update_menu(self, view):
        changed = not self._menu_visible
        if changed:
            self.display.root_group = self._menu_group
            self._menu_visible = True
        formatted = panel.menu_presentation(view)
        for name, _font, _x, _y, _ax, _ay in panel.MENU_DYNAMIC_TEXT:
            value = formatted[name]
            if name == "menu_notice":
                text, colour = value
                font = self._fonts[self._label_fonts[name]]
                while sum(font.get_glyph(ord(char)).shift_x for char in text) > 304:
                    text = text[:-1]
                value = text, colour
            if value != self._last.get(name):
                self.labels[name].text, self.labels[name].color = value
                self._last[name] = value
                changed = True
        instrument = panel.presentation(view)
        width, colour = instrument["load_width"], instrument["load_colour"]
        if (width, colour) != self._last.get("menu_bar"):
            self._menu_bar_group.hidden = width == 0
            self._menu_bar_shape.width = max(1, width)
            self._menu_bar_palette[0] = colour
            self._last["menu_bar"] = width, colour
            changed = True
        if changed:
            self.display.refresh(minimum_frames_per_second=0)
        return changed

    def bounds(self):
        """Diagnostic label rectangles, useful when validating the real panel."""
        result = {}
        for name, item in self.labels.items():
            x, y, width, height = item.bounding_box
            result[name] = (item.x + x, item.y + y, width, height)
        return result
