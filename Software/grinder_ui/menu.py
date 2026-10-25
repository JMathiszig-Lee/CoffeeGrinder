"""Small encoder-driven settings pages; hold SAVE to apply persisted changes."""

from .settings import OPTION_RANGES, validate_options

PAGES = {
    "settings": (("demo", "DEMO"), ("default_rpm", "DEFAULT RPM"),
                 ("autostop", "AUTOSTOP"), ("recovery", "STALL RECOVERY"),
                 ("ack_fault", "ACK FAULT")),
    "autostop": (("autostop_enabled", "ENABLED"), ("autostop_delay_s", "DELAY"),
                 ("unload_percent", "IDLE LOAD"), ("load_hysteresis", "LOAD MARGIN")),
    "recovery": (("stall_recovery_enabled", "ENABLED"), ("stall_percent", "STALL LOAD"),
                 ("stall_detect_s", "DETECT TIME"), ("reverse_rpm", "REVERSE RPM"),
                 ("reverse_duration_s", "REVERSE TIME"), ("recovery_attempts", "MAX ATTEMPTS")),
    "demo": (("run_demo", "RUN DEMO"), ("exit_demo", "EXIT DEMO"),
             ("beans", "ADD BEANS"), ("empty", "EMPTY HOPPER"), ("jam", "SIMULATE JAM")),
}
TITLES = {"settings": "SETTINGS", "autostop": "AUTOSTOP",
          "recovery": "STALL RECOVERY", "demo": "DEMO / TEST"}


def option_text(name, value):
    if name.endswith("_enabled"):
        return "ON" if value else "OFF"
    if name.endswith("_s"):
        return "%.1fs" % value
    if name in ("unload_percent", "load_hysteresis", "stall_percent"):
        return "%d%%" % value
    return str(value)


class SettingsMenu:
    def __init__(self, model):
        self.model = model
        self.page = None
        self.cursor = 0
        self.editing = False
        self.value = None
        self.rpm = model.default_rpm
        self.options = dict(model.options)

    @property
    def dirty(self):
        return self.rpm != self.model.default_rpm or self.options != self.model.options

    def open(self):
        self.page, self.cursor, self.editing = "settings", 0, False

    def back(self):
        if self.editing:
            self.editing = False
        elif self.page == "settings":
            self.page = None
            if self.dirty:
                self.model.show_notice("Menu changes pending; MENU to save")
        elif self.page:
            parent = {"demo": 0, "autostop": 2, "recovery": 3}[self.page]
            self.page, self.cursor = "settings", parent

    def _key(self):
        return PAGES[self.page][self.cursor][0]

    def turn(self, delta):
        if not self.page or not delta:
            return
        if not self.editing:
            self.cursor = max(0, min(len(PAGES[self.page]) - 1, self.cursor + delta))
            return
        key = self._key()
        if key.endswith("_enabled"):
            self.value = delta > 0
        else:
            low, high = (5, 110) if key == "default_rpm" else OPTION_RANGES[key]
            step = 0.1 if key.endswith("_s") else 1
            self.value = max(low, min(high, self.value + delta * step))
            self.value = round(self.value, 1) if key.endswith("_s") else int(self.value)

    def select(self):
        if not self.page:
            return
        key = self._key()
        if self.editing:
            try:
                if key == "default_rpm":
                    self.rpm = self.value
                else:
                    self.options = validate_options({key: self.value}, self.options)
                self.editing = False
                self.model.show_notice("Hold SAVE to apply settings")
            except ValueError as error:
                self.model.show_notice(str(error))
            return
        if key in ("demo", "autostop", "recovery"):
            self.page, self.cursor = key, 0
        elif key in ("run_demo", "exit_demo", "beans", "empty", "jam"):
            self.model.demo_action(key)
        elif key == "ack_fault":
            self.model.acknowledge_automation()
        elif self.model.can_edit_settings():
            self.value = self.rpm if key == "default_rpm" else self.options[key]
            self.editing = True
        else:
            self.model.show_notice("Stop motor before editing settings")

    def view(self):
        if not self.page:
            return None
        first = max(0, self.cursor - 4)
        rows = []
        for index, (key, label) in enumerate(PAGES[self.page][first:first + 5], first):
            value = ""
            if key == "default_rpm" or key in self.options:
                raw = self.rpm if key == "default_rpm" else self.options[key]
                if index == self.cursor and self.editing:
                    raw = self.value
                value = option_text(key, raw)
            elif key in ("demo", "autostop", "recovery"):
                value = ">"
            rows.append((label, value, index == self.cursor, index == self.cursor and self.editing))
        return {"title": TITLES[self.page], "rows": rows, "editing": self.editing,
                "dirty": self.dirty, "position": "%d/%d" % (self.cursor + 1, len(PAGES[self.page])),
                "hint": "SAVE TO APPLY" if self.dirty else "TURN / PRESS TO SELECT"}
