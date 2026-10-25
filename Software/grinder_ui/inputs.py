"""Debounced input routing; stop wins and saving requires an intentional hold."""


class Button:
    def __init__(self, initial=False, now=0.0):
        self.raw = self.stable = bool(initial)
        self.changed_at = now
        self.pressed_at = now if initial else None
        self.held = False

    def update(self, raw, now):
        raw = bool(raw)
        if raw != self.raw:
            self.raw, self.changed_at = raw, now
        pressed = False
        if self.raw != self.stable and now - self.changed_at >= 0.025:
            self.stable = self.raw
            self.pressed_at = now if self.stable else None
            self.held = False
            pressed = self.stable
        hold = False
        if (self.stable and self.raw and not self.held and self.pressed_at is not None
                and now - self.pressed_at >= 1.0):
            self.held, hold = True, True
        return pressed, hold


class Controls:
    def __init__(self, model, position=0, pressed=False, buttons=(False,) * 4, now=0.0):
        self.model, self.position = model, position
        self.knob = Button(pressed, now)
        self.buttons = [Button(raw, now) for raw in buttons]
        self._stop_held = False

    def update(self, position, pressed, buttons, now):
        knob_press, _ = self.knob.update(pressed, now)
        events = [button.update(raw, now) for button, raw in zip(self.buttons, buttons)]
        delta = position - self.position
        self.position = position
        # Holding STOP also inhibits other controls. Do not repeatedly restart
        # the asynchronous stop transaction while the button remains held.
        if self.buttons[1].raw or self.buttons[1].stable:
            # Cancel recovery on the first observed STOP level. Debouncing is
            # useful for starts; delaying a stop would permit a pending restart.
            if not self._stop_held:
                self.model.stop()
                self._stop_held = True
            return
        self._stop_held = False
        if abs(delta) > 32:
            self.model.set_input_error("Encoder position reset; restart UI")
            return
        if events[0][0]:
            if self.model.menu.page:
                self.model.menu.back()
            else:
                self.model.menu.open()
            return
        if delta:
            if self.model.menu.page:
                self.model.menu.turn(delta)
            else:
                self.model.turn(delta)
        if events[2][0]:
            if self.model.menu.page:
                self.model.menu.select()
                return  # One selection per sample, including X + knob together.
            else:
                self.model.recall_default()
        if events[3][0]:
            self.model.show_notice("Hold Y 1 second to save settings" if self.model.menu.page
                                   else "Hold Y 1 second to save default")
        if events[3][1]:
            if self.model.menu.page:
                self.model.save_menu()
            else:
                self.model.save_default()
        if knob_press:
            if self.model.menu.page:
                self.model.menu.select()
            else:
                self.model.press()
