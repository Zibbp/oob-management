# Version 2

Uses a RPI Pico (rp2040) + a W5500 ethernet over SPI.

## Upload Python code to Raspberry Pi Pico

Use mpremote to deploy code:

1. Upload file:
   mpremote connect auto fs cp main.py :

2. Reset device:
   mpremote connect auto reset
