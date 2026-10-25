"""Engine-instrument geometry shared by the LCD and desktop preview.

Side legends align with the PIM715 button centres at y=60 and y=180.
Actual RPM alone drives the grey sector. Missing data never becomes zero.
"""

import math

WIDTH, HEIGHT = 320, 240
BG = 0x080C10
WHITE = 0xEEF3F4
DIM = 0xA0ADB8
LINE = 0x66717A
CYAN = 0x68D8E8
AMBER = 0xFFBE55
RED = 0xFF6464
GREEN = 0x83D9AB
FILL = 0x50565C
COLOURS = (BG, WHITE, DIM, LINE, CYAN, AMBER, RED, GREEN, FILL)
CX, CY = 148, 115
RADIUS = 74
SCALE_MAX = 120
SWEEP = 240
READOUT = (143, 62, 116, 48)
BAR_X, BAR_Y, BAR_W, BAR_H = 81, 204, 170, 7


def point(rpm, radius):
    angle = math.radians(SWEEP * max(0, min(SCALE_MAX, rpm)) / SCALE_MAX)
    return (round(CX + radius * math.cos(angle)), round(CY + radius * math.sin(angle)))


# Twelve-degree arc segments stay within half a pixel of the 74 px circle.
# Keeping their vertices fixed also avoids reshaping the whole fan each frame.
_SECTOR_STEP = 6
_SECTOR_ARC = tuple(point(rpm, RADIUS) for rpm in range(0, SCALE_MAX + 1, _SECTOR_STEP))


def sector(rpm):
    """A clockwise fan from zero to actual RPM; no polygon for zero/unknown."""
    if rpm is None or rpm <= 0:
        return None
    bounded = max(0, min(SCALE_MAX, rpm))
    last = int(bounded // _SECTOR_STEP)
    points = [(CX, CY)] + list(_SECTOR_ARC[:last + 1])
    current = point(bounded, RADIUS)
    if current != points[-1]:
        points.append(current)
    # At very low RPM, pixel rounding can collapse the whole triangle.
    return tuple(points) if len(points) >= 3 and points[1] != points[-1] else None


def pointer(rpm, selected=False):
    if rpm is None:
        return None
    angle = math.radians(SWEEP * max(0, min(SCALE_MAX, rpm)) / SCALE_MAX)
    if selected:
        tip, back = point(rpm, 77), point(rpm, 84)
        dx, dy = round(-math.sin(angle) * 3), round(math.cos(angle) * 3)
        return (tip, (back[0] + dx, back[1] + dy), (back[0] - dx, back[1] - dy))
    start, end = (CX, CY), point(rpm, RADIUS)
    dx, dy = round(-math.sin(angle)), round(math.cos(angle))
    return ((start[0] + dx, start[1] + dy), (end[0] + dx, end[1] + dy),
            (end[0] - dx, end[1] - dy), (start[0] - dx, start[1] - dy))


def static_lines():
    result = []
    previous = point(0, RADIUS)
    for rpm in range(1, 121):
        current = point(rpm, RADIUS)
        result.append(previous + current + (WHITE,))
        previous = current
    for rpm in range(0, 121, 10):
        result.append(point(rpm, RADIUS - (8 if rpm % 20 == 0 else 4))
                      + point(rpm, RADIUS) + (WHITE,))
    # Short leaders associate the text with the bezel, as on an MFD.
    for y in (60, 180):
        result.extend(((0, y, 4, y, DIM), (315, y, 319, y, DIM)))
    return result


# (name, text, font, x, y, horizontal anchor, vertical anchor, colour)
STATIC_TEXT = [
    ("selected_title", "SET", "B612-Regular-12", 145, 42, 0, .5, CYAN),
    ("unit", "RPM", "B612-Regular-14", 250, 140, 1, .5, CYAN),
    ("load_title", "LOAD", "B612-Regular-12", BAR_X, 194, 0, .5, DIM),
    ("button_b", "STOP", "B612-Regular-12", 8, 180, 0, .5, WHITE),
    ("button_x", "DFLT", "B612-Regular-12", 312, 60, 1, .5, WHITE),
    ("button_y", "SAVE", "B612-Regular-12", 312, 180, 1, .5, WHITE),
    ("save_hold", "HOLD 1s", "B612-Regular-12", 312, 194, 1, .5, DIM),
]
for _rpm in range(0, 121, 20):
    _x, _y = point(_rpm, RADIUS - 17)
    if _rpm == 0:
        _y += 12  # Under the readout/zero radial rather than through its border.
    STATIC_TEXT.append(("tick_%d" % _rpm, str(_rpm), "B612-Regular-12", _x, _y, .5, .5, WHITE))

DYNAMIC_TEXT = [
    ("mode", "B612-Regular-12", 160, 12, .5, .5),
    ("actual", "B612Mono-Regular-52", 252, 86, 1, .5),
    ("selected", "B612Mono-Regular-36", 252, 42, 1, .5),
    ("button_a", "B612-Regular-12", 8, 60, 0, .5),
    ("default", "B612-Regular-12", 312, 74, 1, .5),
    ("percent", "B612-Regular-12", BAR_X + BAR_W, 194, 1, .5),
    ("notice", "B612-Regular-12", 160, 220, .5, .5),
    ("status", "B612-Regular-12", 160, 234, .5, .5),
    ("auto_flag", "B612-Regular-12", 8, 12, 0, .5),
    ("recovery_flag", "B612-Regular-12", 312, 12, 1, .5),
]

MENU_STATIC_TEXT = [
    ("menu_back", "BACK", "B612-Regular-12", 8, 60, 0, .5, WHITE),
    ("menu_stop", "STOP", "B612-Regular-12", 8, 180, 0, .5, WHITE),
    ("menu_save", "SAVE", "B612-Regular-12", 312, 180, 1, .5, WHITE),
    ("menu_hold", "HOLD 1s", "B612-Regular-12", 312, 194, 1, .5, DIM),
    ("menu_load", "LOAD", "B612-Regular-12", BAR_X, 194, 0, .5, DIM),
]
MENU_DYNAMIC_TEXT = [
    ("menu_mode", "B612-Regular-12", 160, 12, .5, .5),
    ("menu_title", "B612-Regular-14", 160, 32, .5, .5),
    ("menu_position", "B612-Regular-12", 248, 45, 1, .5),
    ("menu_select", "B612-Regular-12", 312, 60, 1, .5),
    ("menu_rpm", "B612-Regular-12", 160, 177, .5, .5),
    ("menu_percent", "B612-Regular-12", BAR_X + BAR_W, 194, 1, .5),
    ("menu_notice", "B612-Regular-12", 160, 220, .5, .5),
    ("menu_status", "B612-Regular-12", 160, 234, .5, .5),
]
for _index in range(5):
    _y = 58 + 24 * _index
    MENU_DYNAMIC_TEXT.extend([
        ("menu_mark_%d" % _index, "B612-Regular-12", 63, _y, 0, .5),
        ("menu_label_%d" % _index, "B612-Regular-12", 73, _y, 0, .5),
        ("menu_value_%d" % _index, "B612-Regular-12", 248, _y, 1, .5),
    ])


def presentation(view):
    mode, state = view["mode"], view["state"]
    fault = view.get("input_error") or view.get("fault")
    actual, load = view["actual_rpm"], view["load_percent"]
    if fault:
        actual = load = None
    mode_text = {"offline": "CAN OFFLINE", "simulation": "SIMULATION", "live": "LIVE"}[mode]
    if mode == "live" and actual is None:
        mode_text = "LIVE / NO DATA"
    state_text = {"idle": "STOPPED", "starting": "STARTING", "running": "RUNNING",
                  "stopping": "STOPPING", "connecting": "CONNECTING", "fault": "FAULT"}.get(state, "")
    state_colour = GREEN if state == "running" else DIM
    if fault:
        state_text, state_colour = "INPUT FAULT" if view.get("input_error") else "DRIVE FAULT", RED
    high = load is not None and load >= 80
    load_colour = AMBER if high else WHITE
    percent = "-- %" if load is None else ("HIGH %d%%" if high else "%d%%") % round(load)
    notice = view.get("notice") or ""
    automation = view.get("automation") or {}
    if automation.get("message"):
        notice = automation["message"]
    phase = automation.get("phase")
    if not fault and phase in ("stopping_reverse", "reverse", "stopping_resume", "resuming"):
        state_text, state_colour = "STALL RECOVERY", AMBER
    elif not fault and automation.get("remaining_s") is not None:
        state_text, state_colour = "AUTOSTOP", CYAN
    # The permanent mode label replaces redundant mode/help messages.
    if notice.startswith("DEMO:") or notice.lower().startswith("motor offline"):
        notice = ""
    if fault:
        notice = str(fault)
    notice = "".join(char if 32 <= ord(char) < 127 else "?" for char in str(notice).upper())[:38]
    return {
        "mode": (mode_text, AMBER if mode != "live" or actual is None else DIM),
        "actual": ("---" if actual is None else "%03d" % max(-99, min(999, round(actual))), WHITE),
        "selected": ("%03d" % view["set_rpm"], CYAN),
        "button_a": ("MENU", WHITE),
        "default": (str(view["default_rpm"]), DIM),
        "status": (state_text, state_colour),
        "percent": (percent, load_colour if load is not None else DIM),
        "notice": (notice, RED if fault else DIM),
        "actual_sector": sector(abs(actual) if actual is not None else None),
        "actual_pointer": pointer(abs(actual) if actual is not None else None),
        "selected_pointer": pointer(view["set_rpm"], selected=True),
        "load_width": 0 if load is None else round((BAR_W - 4) * max(0, min(100, load)) / 100),
        "load_colour": load_colour,
        "fault": bool(fault),
        "auto_flag": ("AUTO" if view.get("options", {}).get("autostop_enabled") else "", CYAN),
        "recovery_flag": ("RECOV" if view.get("options", {}).get("stall_recovery_enabled") else "", CYAN),
    }


def menu_presentation(view):
    menu = view["menu"]
    instrument = presentation(view)
    actual = view["actual_rpm"]
    result = {
        "menu_mode": instrument["mode"],
        "menu_title": (menu["title"], WHITE),
        "menu_position": (menu["position"], DIM),
        "menu_select": ("SET" if menu["editing"] else "SELECT", WHITE),
        "menu_rpm": ("RPM %s / SET %03d" % ("---" if actual is None else "%03d" % round(actual),
                                              view["set_rpm"]), CYAN),
        "menu_percent": instrument["percent"],
        "menu_notice": instrument["notice"],
        "menu_status": (menu["hint"], AMBER if menu["dirty"] else DIM),
    }
    # Faults and automation state stay visible even while browsing settings.
    if instrument["fault"] or instrument["status"][0] in ("AUTOSTOP", "STALL RECOVERY"):
        result["menu_status"] = instrument["status"]
    for index in range(5):
        row = menu["rows"][index] if index < len(menu["rows"]) else ("", "", False, False)
        label, value, selected, editing = row
        colour = AMBER if editing else CYAN if selected else WHITE
        result["menu_mark_%d" % index] = ("*" if editing else ">" if selected else "", colour)
        result["menu_label_%d" % index] = (label, colour)
        result["menu_value_%d" % index] = (value, colour)
    return result
