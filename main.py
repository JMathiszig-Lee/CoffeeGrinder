from rotaryio import IncrementalEncoder
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY_2, PEN_RGB565
from seesaw_micropython import Seesaw
from digitalio import DigitalIO, Direction, Pull
from machine import I2C, Pin
import time

# Display
display = PicoGraphics(display=DISPLAY_PICO_DISPLAY_2, pen_type=PEN_RGB565, rotate=90)
WIDTH, HEIGHT = display.get_bounds()
BLACK = display.create_pen(0, 0, 0)
RED = display.create_pen(255, 0, 0)
GREEN = display.create_pen(0, 255, 0)
BLUE = display.create_pen(0, 0, 255)
WHITE = display.create_pen(255, 255, 255)
PURPLE = display.create_pen(255, 0, 255)

# Set RPM values
set_rpm = 300
current_rpm = 0


i2c = I2C(0, scl=Pin(5), sda=Pin(4))
ss = Seesaw(i2c, addr=0x36)
ss.pin_mode(24, ss.INPUT_PULLUP)
button = DigitalIO(ss, 24)


button.pull = Pull.UP

encoder = IncrementalEncoder(ss)
rpm_edit_mode = False
last_button_state = True

def read_encoder():
    global set_rpm
    encoder_increment = 10
    delta = encoder.delta
    if delta and rpm_edit_mode:
        set_rpm = max(0, min(1000, set_rpm + delta * encoder_increment))
    
def update_display():
    display.set_pen(WHITE)
    display.clear()
    display.set_pen(GREEN)
    display.rectangle(5,5, WIDTH-10, 70)
    display.set_pen(BLACK)
    display.text("RPM", 10, 5, scale=4)
    display.set_pen(RED)
    display.text(f"Current: {current_rpm}", 10, 30, WIDTH, 3)
    
    if rpm_edit_mode:
        display.set_pen(BLACK)
        display.rectangle(5,50, 150, 20)
        display.set_pen(WHITE) 
    else:
        display.set_pen(BLUE)
    display.text(f"Set: {set_rpm}", 10, 50, WIDTH, 3)
    
    display.update()
    
    

while True:
    
    current_button_state = button.value
    # Detect falling edge (press)
    if not current_button_state and last_button_state:
        rpm_edit_mode = not rpm_edit_mode
        print("RPM edit mode:", rpm_edit_mode)

    last_button_state = current_button_state
        
    current_rpm = 100
    read_encoder()
    update_display()
    time.sleep(0.1)