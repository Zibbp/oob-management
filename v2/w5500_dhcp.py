import network
import time
from machine import Pin, SPI

# Raspberry Pi Pico to W5500 / USR-ES1 wiring used by this example:
#
# Pico 3V3(OUT) -> USR-ES1 VCC
# Pico GND      -> USR-ES1 GND
# Pico GP18     -> USR-ES1 SCLK / SCK
# Pico GP19     -> USR-ES1 MOSI
# Pico GP16     -> USR-ES1 MISO
# Pico GP17     -> USR-ES1 SCS / CS / NSS
# Pico GP20     -> USR-ES1 RST / RESET
#
# INT is not required for this basic DHCP example.

SPI_ID = 0
PIN_SCK = 18
PIN_MOSI = 19
PIN_MISO = 16
PIN_CS = 17
PIN_RST = 20

DHCP_TIMEOUT_SECONDS = 30


def get_ipv4_config(nic):
    """Return (ip, subnet, gateway, dns) across MicroPython API versions."""
    try:
        ip, subnet = nic.ipconfig("addr4")
        gateway = nic.ipconfig("gw4")
        try:
            dns = network.ipconfig("dns")
        except Exception:
            dns = "unknown"
        return ip, subnet, gateway, dns
    except Exception:
        return nic.ifconfig()


def has_ipv4_address(nic):
    ip, _, _, _ = get_ipv4_config(nic)
    return ip not in ("0.0.0.0", None, "")


def connect_w5500_dhcp():
    print("Starting W5500 / USR-ES1 Ethernet...")

    spi = SPI(
        SPI_ID,
        baudrate=2_000_000,
        polarity=0,
        phase=0,
        sck=Pin(PIN_SCK),
        mosi=Pin(PIN_MOSI),
        miso=Pin(PIN_MISO),
    )

    nic = network.WIZNET5K(spi, Pin(PIN_CS), Pin(PIN_RST))

    try:
        network.hostname("pico-w5500")
    except Exception:
        pass

    nic.active(True)

    try:
        nic.ipconfig(dhcp4=True)
        print("DHCP requested with ipconfig(dhcp4=True)")
    except Exception:
        print("ipconfig(dhcp4=True) unavailable; waiting for default DHCP")

    deadline = time.time() + DHCP_TIMEOUT_SECONDS
    while time.time() < deadline:
        link_up = nic.isconnected()
        if link_up and has_ipv4_address(nic):
            ip, subnet, gateway, dns = get_ipv4_config(nic)
            print("Ethernet connected")
            print("IP address:", ip)
            print("Subnet:", subnet)
            print("Gateway:", gateway)
            print("DNS:", dns)
            return nic

        print("Waiting for link/DHCP...")
        time.sleep(1)

    print("Ethernet link up:", nic.isconnected())
    print("Current network config:", get_ipv4_config(nic))
    raise RuntimeError("Timed out waiting for Ethernet DHCP lease")


nic = connect_w5500_dhcp()
