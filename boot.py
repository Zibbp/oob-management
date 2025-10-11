import gc, machine

# LED pins
powerLED = machine.Pin(3, machine.Pin.OUT)
heartbeatLED = machine.Pin(2, machine.Pin.OUT)

# Power LED is a gpio pin so we know if the device is running the custom firmware
powerLED.value(1)
# Turn off heartbeat LED until main.py takes over
heartbeatLED.value(0)

try:
    import main
finally:
    gc.collect()
