upload:
	uv tool run --from adafruit-ampy ampy --port /dev/ttyACM0 put boot.py
	uv tool run --from adafruit-ampy ampy --port /dev/ttyACM0 put main.py
	uv tool run --from adafruit-ampy ampy --port /dev/ttyACM0 put index.html
	uv tool run esptool --port /dev/ttyACM0 --before default-reset --after hard-reset chip-id

erase_flash:
	uv tool run esptool --port /dev/ttyACM0 erase_flash

shell:
	screen /dev/ttyACM0 19200

flash_micropython:
	curl https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20250911-v1.26.1.bin -o esp32c3.bin
	uv tool run esptool --port /dev/ttyACM0 --baud 460800 write_flash 0 esp32c3.bin
