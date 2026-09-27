# oob-firmware

Out-of-band ATX power management for 4 PCs, running on a Raspberry Pi Pico (RP2040) with W5500 Ethernet. TinyGo port of the MicroPython setup in `old/`.

Web UI for power/reset/force-off per PC, KVM switching, editable labels, login change, and an activity log. No OTA — firmware updates are USB-only.

## Hardware

| Part | Connection |
| ---- | ---------- |
| W5500 Ethernet | SPI0: SCK GP18, MOSI GP19, MISO GP16, CS GP17, RST GP20 |
| MCP23017 I/O expander | I2C1: SDA GP2, SCL GP3, addr `0x20`, 100 kHz |
| KVM switch (TX only) | GP5, 19200 baud 8N1, sends `G0XdA` |
| External device console | UART0: TX GP0, RX GP1, 115200 baud 8N1 (`SERIAL_*` in `config.go`) |
| Controller LED | GP6 + Pico onboard LED (solid when ready, blink while waiting for network) |

Each PC uses 4 MCP23017 pins: power-switch pulse (out), reset-switch pulse (out), motherboard power-LED sense (in, active-low), aux sense (in, active-low).

| PC | Bank | Power | Reset | LED | Aux |
| -- | ---- | ----- | ----- | --- | --- |
| 1 | A | 0 | 1 | 2 | 3 |
| 2 | A | 4 | 5 | 6 | 7 |
| 3 | B | 0 | 1 | 2 | 3 |
| 4 | B | 4 | 5 | 6 | 7 |

Pulse lengths: power 1.0 s, reset 0.7 s, force-off (hold) 5 s. Pin map and timings live in `config.go`.

## Network

- DHCP only (`hostname oob-pico`). No link/lease → both LEDs blink and it retries forever. There is no static-IP fallback.
- MAC is locally administered, derived from the flash ID: `02:4F:4F:42:XX:XX`.
- HTTP on port 80. Basic auth (default `oob` / `oob`), changeable in the Web UI; credentials and labels persist across reboots *and* USB reflashes (dedicated flash sector).

## Web UI

`http://<dhcp-ip>/` (try `http://oob-pico/` if your LAN resolves hostnames). Per-PC cards with live power/aux state, Power / Reset / Hold-5s / KVM buttons, click-to-rename labels, activity log, access settings. Tick the `serial` checkbox in the header to show the external-device console (output view, send box, clear).

API (all except `/health` need Basic auth):
| Endpoint | Method | Notes |
| -------- | ------ | ----- |
| `/` | GET | UI |
| `/status` | GET | JSON: version, ip, link, per-PC state |
| `/health` | GET | `{"ok":true}` — no auth |
| `/log` | GET | plain-text ring (80 entries) |
| `/action` | POST | `pc=1..4`, `action=power\|reset\|force_off` (`poweron`/`poweroff` aliases) |
| `/kvm` | POST | `port=1..4` |
| `/label` (`/setlabel`) | POST | `pc=1..4`, `name=≤32 chars` |
| `/auth` | POST | `user=≤32 chars`, `pass=≤64 chars` |
| `/serial` | GET | buffered external-device output, plain text |
| `/serial/send` | POST | `text=…` writes to the device UART |
| `/serial/clear` | POST | clears the serial buffer |

## Layout

```
main.go      boot + supervise network → serve forever
config.go    pins, PC table, timings, defaults
netbring.go  W5500 init + minimal DHCP client
mcp.go       MCP23017 outputs/inputs, ATX actions
kvm.go       bit-banged 19200 TX
serial.go    UART0 device console (RX ring + TX)
store.go     labels + credentials in flash
server.go    self-healing HTTP server (see below)
web.go       routes + auth
ui.html      embedded UI (//go:embed)
```

Upstream deps only: `tinygo.org/x/drivers` (w5500, mcp23017) plus `github.com/tinygo-org/pio` for the KVM UART (bit-exact port of the old MicroPython PIO program). The stock `net/http` server dies permanently on transient W5500 accept errors, so `server.go` runs its own accept loop that re-creates a wedged listener instead. The W5500 accept path has a millisecond deaf window per connection, so the UI staggers polls and retries once silently — actions never retry (no double power-presses).

## Build & flash

Requirements: TinyGo ≥ 0.42.

```sh
# inspect
tinygo build -target pico -o firmware.elf .

# flash over USB (Pico in BOOTSEL: hold BOOTSEL, tap RESET / replug)
tinygo flash -target pico .

# or copy the UF2 manually
tinygo build -target pico -o firmware.uf2 .
# then drop firmware.uf2 onto the RPI-RP2 mass-storage drive
```

Serial log (115200 baud) shows DHCP and HTTP progress, e.g.:

```
[2720] link up: 100Mbps Full Duplex
[2727] net: ip=197.100.1.189 mask=255.255.255.0 gw=197.100.1.1
[2728] listening on http://197.100.1.189:80/
```

Note: `tinygo flash` on a running device needs serial-port access to reset it into BOOTSEL; without permission, enter BOOTSEL manually.
