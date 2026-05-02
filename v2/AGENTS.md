## Repository and Code base information

This is a micropython codebase. The connected device is a RPI Pico (rp2040) connected to a w5500 (USR-ES1).

## GPIO Connections

Connections to the Pico:

Serial Port
- GPIO4 (D+)
- GPIO5 (D-)

MCP23017x IO Expander:

SCK: GPIO3
SDA: GPIO2
A0/A1/A2: GRND

PowerLED: GPIO6

MCP Expander to each rj45 Jack:

GPA0: RJ45_CONN1_PORT2
GPA1: RJ45_CONN1_PORT3
GPA2: RJ45_CONN1_PORT4
GPA3: RJ45_CONN1_PORT8

Repeated for the remaining 3.

## Upload Python code to Raspberry Pi Pico

Use mpremote to deploy code:

1. Upload file:
   mpremote connect auto fs cp main.py :

2. Reset device:
   mpremote connect auto reset
