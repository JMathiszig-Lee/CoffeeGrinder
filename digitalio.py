# MicroPython-compatible DigitalIO for Adafruit Seesaw GPIO

class Direction:
    INPUT = 0
    OUTPUT = 1

class Pull:
    UP = 0
    DOWN = 1
    NONE = None

class DriveMode:
    PUSH_PULL = 0

class DigitalIO:
    def __init__(self, seesaw, pin):
        self._seesaw = seesaw
        self._pin = pin
        self._drive_mode = DriveMode.PUSH_PULL
        self._direction = Direction.INPUT
        self._pull = None
        self._value = False

    def deinit(self):
        pass

    def switch_to_output(self, value=False, drive_mode=DriveMode.PUSH_PULL):
        self._seesaw.pin_mode(self._pin, self._seesaw.OUTPUT)
        self._seesaw.digital_write(self._pin, value)
        self._drive_mode = drive_mode
        self._pull = None
        self._value = value

    def switch_to_input(self, pull=None):
        if pull == Pull.DOWN:
            self._seesaw.pin_mode(self._pin, self._seesaw.INPUT_PULLDOWN)
        elif pull == Pull.UP:
            self._seesaw.pin_mode(self._pin, self._seesaw.INPUT_PULLUP)
        else:
            self._seesaw.pin_mode(self._pin, self._seesaw.INPUT)
        self._pull = pull

    @property
    def direction(self):
        return self._direction

    @direction.setter
    def direction(self, value):
        if value == Direction.OUTPUT:
            self.switch_to_output()
        elif value == Direction.INPUT:
            self.switch_to_input()
        else:
            raise ValueError("Invalid direction")
        self._direction = value

    @property
    def value(self):
        if self._direction == Direction.OUTPUT:
            return self._value
        return self._seesaw.digital_read(self._pin)

    @value.setter
    def value(self, val):
        if not 0 <= val <= 1:
            raise ValueError("Value must be 0 or 1")
        self._seesaw.digital_write(self._pin, val)
        self._value = val

    @property
    def drive_mode(self):
        return self._drive_mode

    @drive_mode.setter
    def drive_mode(self, mode):
        self._drive_mode = mode

    @property
    def pull(self):
        return self._pull

    @pull.setter
    def pull(self, mode):
        if self._direction == Direction.OUTPUT:
            raise AttributeError("Cannot set pull on an output pin")
        self.switch_to_input(pull=mode)