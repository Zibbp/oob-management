import gc
import os
import socket
import time

try:
    import ujson as json
except ImportError:
    import json

try:
    import ubinascii as binascii
except ImportError:
    import binascii

import network
import machine
from machine import I2C, Pin, SPI

try:
    from rp2 import PIO, StateMachine, asm_pio
except ImportError:
    PIO = None
    StateMachine = None
    asm_pio = None


APP_NAME = "OOB Management"
APP_VERSION = "2.0.0-pico"
HOSTNAME = "oob-pico"
HTTP_PORT = 80
DHCP_TIMEOUT_SECONDS = 30
HTTP_CLIENT_TIMEOUT_SECONDS = 2
HTTP_ACCEPT_TIMEOUT_SECONDS = 1
HTTP_MAX_REQUEST_BYTES = 8192

OTA_ENABLED = True
OTA_MAX_BYTES = 96 * 1024
OTA_MAIN_FILE = "main.py"
OTA_UPLOAD_FILE = "main.py.upload"
OTA_BACKUP_FILE = "main.py.bak"
OTA_PENDING_FILE = "ota_pending.json"
OTA_BOOTING_FILE = "ota_booting"

# Set AUTH_ENABLED to False if this controller lives on a fully isolated network.
AUTH_ENABLED = True
AUTH_USER = "oob"
AUTH_PASS = "oob"

# Raspberry Pi Pico to W5500 / USR-ES1 wiring.
W5500_SPI_ID = 0
W5500_PIN_SCK = 18
W5500_PIN_MOSI = 19
W5500_PIN_MISO = 16
W5500_PIN_CS = 17
W5500_PIN_RST = 20

# Raspberry Pi Pico to MCP23017 wiring.
MCP_I2C_ID = 1
MCP_PIN_SDA = 2
MCP_PIN_SCL = 3
MCP_ADDR = 0x20
MCP_I2C_FREQ = 100000

# Optional transmit-only serial/KVM link using RP2040 PIO.
KVM_PIO_SM_ID = 0
KVM_UART_BAUD = 19200
KVM_PIN_TX = 5

# Controller status LED.
POWER_LED_PIN = 6

# MCP23017 register map.
IODIRA = 0x00
IODIRB = 0x01
GPPUA = 0x0C
GPPUB = 0x0D
GPIOA = 0x12
GPIOB = 0x13
OLATA = 0x14
OLATB = 0x15

# Each RJ45 group uses four MCP pins in this order:
# port 2 = ATX power switch pulse
# port 3 = ATX reset switch pulse
# port 4 = motherboard power LED sense
# port 8 = spare/aux sense
#
# Adjust this table if the harness order changes.
PCS = (
    {
        "id": 1,
        "name": "PC 1",
        "bank": "A",
        "power": 0,
        "reset": 1,
        "power_led": 2,
        "aux": 3,
    },
    {
        "id": 2,
        "name": "PC 2",
        "bank": "A",
        "power": 4,
        "reset": 5,
        "power_led": 6,
        "aux": 7,
    },
    {
        "id": 3,
        "name": "PC 3",
        "bank": "B",
        "power": 0,
        "reset": 1,
        "power_led": 2,
        "aux": 3,
    },
    {
        "id": 4,
        "name": "PC 4",
        "bank": "B",
        "power": 4,
        "reset": 5,
        "power_led": 6,
        "aux": 7,
    },
)

ACTIVE_LOW_SENSE = True
POWER_PULSE_SECONDS = 1.0
RESET_PULSE_SECONDS = 0.7
FORCE_OFF_SECONDS = 5.0
LABELS_FILE = "labels.json"
LOG_MAX_ENTRIES = 80
LOG_MAX_LINE_CHARS = 160


if asm_pio:

    @asm_pio(
        sideset_init=PIO.OUT_HIGH, out_init=PIO.OUT_HIGH, out_shiftdir=PIO.SHIFT_RIGHT
    )
    def pio_uart_tx():
        pull()
        set(x, 7).side(0)[7]
        label("bitloop")
        out(pins, 1)[6]
        jmp(x_dec, "bitloop")
        nop().side(1)[6]

else:
    pio_uart_tx = None


log_lines = []
labels = {}
nic = None
try:
    OTA_WDT
except NameError:
    OTA_WDT = None
controller_led = Pin(POWER_LED_PIN, Pin.OUT)
controller_led.on()


def feed_watchdog():
    if OTA_WDT:
        try:
            OTA_WDT.feed()
        except Exception:
            pass


def sleep_with_watchdog(seconds):
    milliseconds = int(seconds * 1000)
    if milliseconds <= 0:
        feed_watchdog()
        return

    deadline = time.ticks_add(time.ticks_ms(), milliseconds)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        feed_watchdog()
        remaining = time.ticks_diff(deadline, time.ticks_ms())
        time.sleep_ms(min(1000, max(1, remaining)))
    feed_watchdog()


def file_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def log(message):
    message = str(message)
    if len(message) > LOG_MAX_LINE_CHARS:
        message = message[: LOG_MAX_LINE_CHARS - 3] + "..."
    line = "[{}] {}".format(time.ticks_ms(), message)
    print(line)
    log_lines.append(line)
    while len(log_lines) > LOG_MAX_ENTRIES:
        log_lines.pop(0)


def log_text():
    if not log_lines:
        return ""
    return "\n".join(log_lines) + "\n"


def sync_filesystem():
    try:
        os.sync()
    except Exception:
        pass


def get_ipv4_config(iface):
    try:
        ip, subnet = iface.ipconfig("addr4")
        gateway = iface.ipconfig("gw4")
        try:
            dns = network.ipconfig("dns")
        except Exception:
            dns = "unknown"
        return ip, subnet, gateway, dns
    except Exception:
        return iface.ifconfig()


def has_ipv4_address(iface):
    ip, _, _, _ = get_ipv4_config(iface)
    return ip not in ("0.0.0.0", None, "")


def connect_w5500_dhcp():
    feed_watchdog()
    log("Starting W5500 Ethernet")
    spi = SPI(
        W5500_SPI_ID,
        baudrate=2_000_000,
        polarity=0,
        phase=0,
        sck=Pin(W5500_PIN_SCK),
        mosi=Pin(W5500_PIN_MOSI),
        miso=Pin(W5500_PIN_MISO),
    )

    iface = network.WIZNET5K(spi, Pin(W5500_PIN_CS), Pin(W5500_PIN_RST))
    feed_watchdog()

    try:
        network.hostname(HOSTNAME)
    except Exception:
        pass

    iface.active(True)
    feed_watchdog()

    try:
        iface.ipconfig(dhcp4=True)
        log("DHCP requested")
    except Exception:
        log("Waiting for default DHCP")

    deadline = time.time() + DHCP_TIMEOUT_SECONDS
    while time.time() < deadline:
        feed_watchdog()
        if iface.isconnected() and has_ipv4_address(iface):
            ip, subnet, gateway, dns = get_ipv4_config(iface)
            log(
                "Ethernet connected: ip={} subnet={} gateway={} dns={}".format(
                    ip, subnet, gateway, dns
                )
            )
            return iface

        log("Waiting for Ethernet link/DHCP")
        sleep_with_watchdog(1)

    log("Ethernet link up: {}".format(iface.isconnected()))
    log("Network config: {}".format(get_ipv4_config(iface)))
    raise RuntimeError("Timed out waiting for Ethernet DHCP lease")


class MCP23017:
    def __init__(self, i2c, address, bus_name):
        self.i2c = i2c
        self.address = address
        self.bus_name = bus_name
        self.output_latch = {"A": 0, "B": 0}

    def write_reg(self, reg, value):
        self.i2c.writeto_mem(self.address, reg, bytes([value & 0xFF]))

    def read_reg(self, reg):
        return self.i2c.readfrom_mem(self.address, reg, 1)[0]

    def begin(self):
        feed_watchdog()
        found = self.i2c.scan()
        feed_watchdog()
        if self.address not in found:
            mcp_addresses = []
            for address in found:
                if MCP_ADDR_MIN <= address <= MCP_ADDR_MAX:
                    mcp_addresses.append(address)

            if mcp_addresses:
                self.address = mcp_addresses[0]
                log(
                    "MCP23017 expected at 0x{:02X}, using detected address 0x{:02X}".format(
                        MCP_ADDR, self.address
                    )
                )
            else:
                raise RuntimeError(
                    "MCP23017 not found at 0x{:02X} on {}; scan={}. "
                    "Check VDD/VSS, common ground, SDA/SCL wiring, pull-ups, RESET high, "
                    "and A0/A1/A2 address pins.".format(
                        self.address, self.bus_name, found
                    )
                )

        input_mask_a = 0
        input_mask_b = 0
        for pc in PCS:
            mask = (1 << pc["power_led"]) | (1 << pc["aux"])
            if pc["bank"] == "A":
                input_mask_a |= mask
            else:
                input_mask_b |= mask

        self.write_reg(OLATA, 0x00)
        feed_watchdog()
        self.write_reg(OLATB, 0x00)
        self.write_reg(IODIRA, input_mask_a)
        self.write_reg(IODIRB, input_mask_b)
        self.write_reg(GPPUA, input_mask_a)
        self.write_reg(GPPUB, input_mask_b)
        feed_watchdog()
        log(
            "MCP23017 ready: IODIRA=0x{:02X} IODIRB=0x{:02X}".format(
                input_mask_a, input_mask_b
            )
        )

    def gpio_reg(self, bank):
        return GPIOA if bank == "A" else GPIOB

    def olat_reg(self, bank):
        return OLATA if bank == "A" else OLATB

    def read_pin(self, bank, bit):
        value = self.read_reg(self.gpio_reg(bank))
        raw = (value >> bit) & 1
        if ACTIVE_LOW_SENSE:
            return 1 if raw == 0 else 0
        return raw

    def set_output(self, bank, bit, enabled):
        mask = 1 << bit
        if enabled:
            self.output_latch[bank] |= mask
        else:
            self.output_latch[bank] &= ~mask
        self.write_reg(self.olat_reg(bank), self.output_latch[bank])

    def pulse(self, bank, bit, seconds):
        feed_watchdog()
        self.set_output(bank, bit, True)
        sleep_with_watchdog(seconds)
        self.set_output(bank, bit, False)
        feed_watchdog()


def make_mcp():
    feed_watchdog()
    bus_name = "I2C{} SDA=GPIO{} SCL=GPIO{} freq={}".format(
        MCP_I2C_ID, MCP_PIN_SDA, MCP_PIN_SCL, MCP_I2C_FREQ
    )
    i2c = I2C(MCP_I2C_ID, scl=Pin(MCP_PIN_SCL), sda=Pin(MCP_PIN_SDA), freq=MCP_I2C_FREQ)
    expander = MCP23017(i2c, MCP_ADDR, bus_name)
    expander.begin()
    feed_watchdog()
    return expander


class PioUartTx:
    def __init__(self, state_machine_id, pin, baudrate):
        if not (PIO and StateMachine and pio_uart_tx):
            raise RuntimeError("RP2040 PIO is not available")
        tx_pin = Pin(pin)
        self.sm = StateMachine(
            state_machine_id,
            pio_uart_tx,
            freq=8 * baudrate,
            sideset_base=tx_pin,
            out_base=tx_pin,
        )
        self.sm.active(1)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        for byte in data:
            self.sm.put(byte)


def make_kvm_uart():
    feed_watchdog()
    try:
        return PioUartTx(KVM_PIO_SM_ID, KVM_PIN_TX, KVM_UART_BAUD)
    except Exception as exc:
        log("KVM UART disabled: {}".format(exc))
        return None


mcp = make_mcp()
kvm_uart = make_kvm_uart()


def load_labels():
    global labels
    labels = {}
    try:
        with open(LABELS_FILE, "r") as f:
            stored = json.loads(f.read())
        for pc in PCS:
            key = str(pc["id"])
            name = stored.get(key, pc["name"])
            labels[pc["id"]] = name[:32] if name else pc["name"]
        log("Loaded labels")
    except Exception:
        for pc in PCS:
            labels[pc["id"]] = pc["name"]


def save_labels():
    data = {}
    for pc_id, name in labels.items():
        data[str(pc_id)] = name
    with open(LABELS_FILE, "w") as f:
        f.write(json.dumps(data))
    sync_filesystem()
    log("Saved labels")


def find_pc(pc_id):
    for pc in PCS:
        if pc["id"] == pc_id:
            return pc
    return None


def pc_status():
    pcs = []
    for pc in PCS:
        power_on = bool(mcp.read_pin(pc["bank"], pc["power_led"]))
        aux_on = bool(mcp.read_pin(pc["bank"], pc["aux"]))
        pcs.append(
            {
                "id": pc["id"],
                "label": labels.get(pc["id"], pc["name"]),
                "power_on": power_on,
                "aux_on": aux_on,
                "bank": pc["bank"],
                "power_bit": pc["power"],
                "reset_bit": pc["reset"],
                "led_bit": pc["power_led"],
                "aux_bit": pc["aux"],
            }
        )
    return pcs


def kvm_switch(port):
    if not (1 <= port <= len(PCS)):
        raise ValueError("Invalid KVM port")
    if not kvm_uart:
        raise RuntimeError("KVM UART is not available")
    kvm_uart.write("G0{}gA".format(port))
    log("KVM switched to port {}".format(port))


def run_action(pc_id, action):
    pc = find_pc(pc_id)
    if not pc:
        raise ValueError("Unknown PC")

    if action == "poweron":
        action = "power"
    elif action == "poweroff":
        action = "force_off"

    if action == "power":
        log("Power pulse for PC {}".format(pc_id))
        mcp.pulse(pc["bank"], pc["power"], POWER_PULSE_SECONDS)
    elif action == "reset":
        log("Reset pulse for PC {}".format(pc_id))
        mcp.pulse(pc["bank"], pc["reset"], RESET_PULSE_SECONDS)
    elif action == "force_off":
        log("Force-off pulse for PC {}".format(pc_id))
        mcp.pulse(pc["bank"], pc["power"], FORCE_OFF_SECONDS)
    else:
        raise ValueError("Unknown action")


def url_unquote(value):
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "%" and i + 2 < len(value):
            try:
                out.append(chr(int(value[i + 1 : i + 3], 16)))
                i += 3
                continue
            except Exception:
                pass
        out.append(" " if char == "+" else char)
        i += 1
    return "".join(out)


def parse_kv_pairs(value):
    params = {}
    if not value:
        return params
    for item in value.split("&"):
        if not item:
            continue
        if "=" in item:
            key, val = item.split("=", 1)
        else:
            key, val = item, ""
        params[url_unquote(key)] = url_unquote(val)
    return params


def split_path_query(path):
    if "?" not in path:
        return path, {}
    route, query = path.split("?", 1)
    return route, parse_kv_pairs(query)


def parse_content_length(headers):
    try:
        return int(headers.get("content-length", "0") or "0")
    except Exception:
        raise ValueError("Invalid Content-Length")


def read_request_start(client):
    client.settimeout(HTTP_CLIENT_TIMEOUT_SECONDS)
    data = b""
    while b"\r\n\r\n" not in data:
        feed_watchdog()
        chunk = client.recv(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > HTTP_MAX_REQUEST_BYTES:
            raise ValueError("Request headers too large")

    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        return "", "", {}, b""

    try:
        method, path, _proto = lines[0].decode().split(" ", 2)
    except Exception:
        return "", "", {}, b""

    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.decode().strip().lower()] = value.decode().strip()

    return method, path, headers, rest


def read_request_body(client, headers, initial_body, max_bytes=HTTP_MAX_REQUEST_BYTES):
    length = parse_content_length(headers)
    if length > max_bytes:
        raise ValueError("Request body too large")

    body = initial_body[:length]
    while len(body) < length:
        feed_watchdog()
        chunk = client.recv(min(1024, length - len(body)))
        if not chunk:
            break
        body += chunk

    if len(body) != length:
        raise ValueError("Incomplete request body")

    return body


def stream_request_body_to_file(client, headers, initial_body, path, max_bytes):
    length = parse_content_length(headers)
    if length <= 0:
        raise ValueError("Upload is empty")
    if length > max_bytes:
        raise ValueError("Upload is too large")

    written = 0
    with open(path, "wb") as f:
        if initial_body:
            chunk = initial_body[:length]
            f.write(chunk)
            written += len(chunk)
            feed_watchdog()

        while written < length:
            feed_watchdog()
            chunk = client.recv(min(1024, length - written))
            if not chunk:
                raise ValueError("Upload ended early")
            f.write(chunk)
            written += len(chunk)

    sync_filesystem()
    return written


def validate_ota_upload(path):
    feed_watchdog()
    gc.collect()
    source = None
    code = None
    try:
        with open(path, "r") as f:
            source = f.read()
        feed_watchdog()
        code = compile(source, OTA_MAIN_FILE, "exec")
        feed_watchdog()
    except Exception as exc:
        raise ValueError("Upload validation failed: {}".format(exc))
    finally:
        try:
            del code
        except Exception:
            pass
        try:
            del source
        except Exception:
            pass
        gc.collect()


def write_ota_pending(size, remote):
    payload = {
        "file": OTA_MAIN_FILE,
        "backup": OTA_BACKUP_FILE,
        "size": size,
        "remote": remote,
        "version": APP_VERSION,
        "ticks_ms": time.ticks_ms(),
    }
    with open(OTA_PENDING_FILE, "w") as f:
        f.write(json.dumps(payload))
    sync_filesystem()


def install_ota_upload(size, remote):
    main_moved = False
    try:
        remove_file(OTA_BACKUP_FILE)
        write_ota_pending(size, remote)
        os.rename(OTA_MAIN_FILE, OTA_BACKUP_FILE)
        main_moved = True
        os.rename(OTA_UPLOAD_FILE, OTA_MAIN_FILE)
        sync_filesystem()
    except Exception:
        if (
            main_moved
            and file_exists(OTA_BACKUP_FILE)
            and not file_exists(OTA_MAIN_FILE)
        ):
            try:
                os.rename(OTA_BACKUP_FILE, OTA_MAIN_FILE)
            except Exception:
                pass
        remove_file(OTA_PENDING_FILE)
        remove_file(OTA_BOOTING_FILE)
        sync_filesystem()
        raise


def clear_ota_markers_if_healthy():
    if not (file_exists(OTA_PENDING_FILE) or file_exists(OTA_BOOTING_FILE)):
        return
    remove_file(OTA_PENDING_FILE)
    remove_file(OTA_BOOTING_FILE)
    sync_filesystem()
    log("OTA boot accepted; HTTP server is listening")


def handle_ota_update(client, headers, initial_body, remote):
    if not OTA_ENABLED:
        response(client, 403, "text/plain; charset=utf-8", "OTA updates disabled\n")
        return

    remove_file(OTA_UPLOAD_FILE)
    try:
        log("OTA upload started from {}".format(remote))
        size = stream_request_body_to_file(
            client, headers, initial_body, OTA_UPLOAD_FILE, OTA_MAX_BYTES
        )
        validate_ota_upload(OTA_UPLOAD_FILE)
        install_ota_upload(size, remote)
    except ValueError as exc:
        remove_file(OTA_UPLOAD_FILE)
        log("OTA upload rejected from {}: {}".format(remote, exc))
        response(client, 400, "text/plain; charset=utf-8", "{}\n".format(exc))
        return
    except Exception as exc:
        remove_file(OTA_UPLOAD_FILE)
        log("OTA upload failed from {}: {}".format(remote, exc))
        raise

    log("OTA update installed from {}: {} bytes; rebooting".format(remote, size))
    json_response(client, {"ok": True, "size": size, "rebooting": True})
    sleep_with_watchdog(0.3)
    machine.reset()


def is_socket_timeout(exc):
    if isinstance(exc, OSError) and exc.args:
        return exc.args[0] in (11, 110, 116)
    return False


def read_request(client):
    method, path, headers, rest = read_request_start(client)
    if not method:
        return "", "", {}, b""
    return method, path, headers, read_request_body(client, headers, rest)


def send_all(client, data):
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        feed_watchdog()
        n = client.send(view[sent:])
        if n == 0:
            raise RuntimeError("socket connection broken")
        sent += n
    feed_watchdog()


def response(client, code, content_type, body, headers=None):
    if isinstance(body, str):
        body = body.encode("utf-8")

    reason = {
        200: "OK",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(code, "OK")

    extra = ""
    if headers:
        for key, value in headers.items():
            extra += "{}: {}\r\n".format(key, value)

    head = (
        "HTTP/1.1 {} {}\r\n"
        "Content-Type: {}\r\n"
        "Content-Length: {}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "{}\r\n"
    ).format(code, reason, content_type, len(body), extra)
    send_all(client, head.encode("utf-8"))
    if body:
        send_all(client, body)


def json_response(client, payload, code=200):
    response(client, code, "application/json; charset=utf-8", json.dumps(payload))


def is_client_disconnect(exc):
    if isinstance(exc, OSError) and exc.args:
        return exc.args[0] in (32, 103, 104, 110, 116)
    return False


def check_auth(headers):
    if not AUTH_ENABLED:
        return True
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = binascii.a2b_base64(auth[6:]).decode()
        user, password = decoded.split(":", 1)
        return user == AUTH_USER and password == AUTH_PASS
    except Exception:
        return False


def unauthorized(client):
    response(
        client,
        401,
        "text/plain; charset=utf-8",
        "Unauthorized\n",
        {"WWW-Authenticate": 'Basic realm="OOB Management"'},
    )


def remote_label(remote_addr):
    if not remote_addr:
        return "unknown"
    try:
        return str(remote_addr[0])
    except Exception:
        return str(remote_addr)


def status_payload():
    ip, subnet, gateway, dns = get_ipv4_config(nic)
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "hostname": HOSTNAME,
        "ip": ip,
        "subnet": subnet,
        "gateway": gateway,
        "dns": dns,
        "link": bool(nic and nic.isconnected()),
        "heap_free": gc.mem_free() if hasattr(gc, "mem_free") else None,
        "pcs": pc_status(),
    }


def html_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def index_page():
    ip, _, _, _ = get_ipv4_config(nic)
    cards = []
    for pc in PCS:
        pc_id = pc["id"]
        label = html_escape(labels.get(pc_id, pc["name"]))
        cards.append(
            """
      <article class="pc-card" data-pc="{id}">
        <header>
          <div>
            <input class="label-input" maxlength="32" value="{label}" aria-label="PC {id} label">
            <div class="pin-map">MCP GP{bank}{power}/GP{bank}{reset} control, GP{bank}{led} LED</div>
          </div>
          <span class="state" data-state>Unknown</span>
        </header>
        <div class="meter"><span data-meter></span></div>
        <div class="signals">
          <span class="signal"><b data-led>--</b> Power LED</span>
          <span class="signal bravo-signal" data-bravo-signal><b data-aux>--</b> Bravo board</span>
        </div>
        <div class="actions">
          <button class="primary" data-action="power">Power</button>
          <button data-action="reset">Reset</button>
          <button class="danger" data-action="force_off">Hold Power</button>
          <button data-kvm>Switch KVM</button>
        </div>
      </article>""".format(
                id=pc_id,
                label=label,
                bank=pc["bank"],
                power=pc["power"],
                reset=pc["reset"],
                led=pc["power_led"],
            )
        )

    auth_note = "Basic auth enabled" if AUTH_ENABLED else "Auth disabled"
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{app}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #111418;
      --panel: #1b2027;
      --panel-2: #222934;
      --text: #f4f7fb;
      --muted: #9ca9b7;
      --line: #303946;
      --green: #45d483;
      --red: #ff6b70;
      --amber: #f2b84b;
      --blue: #63b3ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px clamp(16px, 4vw, 44px);
      border-bottom: 1px solid var(--line);
      background: #151a20;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ margin: 0; font-size: 22px; font-weight: 760; letter-spacing: 0; }}
    .subline {{ margin-top: 4px; color: var(--muted); font-size: 13px; }}
    .net {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      color: var(--muted);
      font-size: 13px;
    }}
    .chip {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 7px 9px;
      white-space: nowrap;
    }}
    main {{ padding: 26px clamp(16px, 4vw, 44px) 36px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      align-items: stretch;
    }}
    .pc-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-height: 238px;
      box-shadow: 0 12px 34px rgba(0,0,0,.22);
      transition: border-color .2s ease, box-shadow .2s ease;
    }}
    .pc-card.bravo-connected {{
      border-color: rgba(69,212,131,.72);
      box-shadow: 0 0 0 1px rgba(69,212,131,.14), 0 16px 36px rgba(0,0,0,.28);
    }}
    .pc-card header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      min-height: 58px;
    }}
    .label-input {{
      width: 100%;
      min-width: 0;
      border: 0;
      outline: 0;
      border-bottom: 1px solid transparent;
      background: transparent;
      color: var(--text);
      font: inherit;
      font-size: 20px;
      font-weight: 720;
      padding: 0 0 3px;
    }}
    .label-input:focus {{ border-bottom-color: var(--blue); }}
    .pin-map {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .state {{
      flex: 0 0 auto;
      border-radius: 999px;
      padding: 5px 8px;
      font-size: 12px;
      font-weight: 700;
      background: #2a3038;
      color: var(--muted);
    }}
    .state.on {{ background: rgba(69,212,131,.13); color: var(--green); }}
    .state.off {{ background: rgba(255,107,112,.12); color: var(--red); }}
    .meter {{
      height: 9px;
      border-radius: 999px;
      background: #0f1216;
      overflow: hidden;
      margin: 18px 0;
      border: 1px solid #252c35;
    }}
    .meter span {{
      display: block;
      width: 18%;
      height: 100%;
      background: var(--red);
      transition: width .2s ease, background .2s ease;
    }}
    .meter span.on {{ width: 100%; background: var(--green); }}
    .meter span.off {{ width: 100%; background: var(--red); }}
    .signals {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 16px;
    }}
    .signal {{
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
    }}
    .signals b {{ display: block; color: var(--text); font-size: 18px; margin-bottom: 2px; }}
    .bravo-signal {{
      position: relative;
      padding-left: 36px;
    }}
    .bravo-signal::before {{
      content: "";
      position: absolute;
      left: 12px;
      top: 15px;
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: var(--red);
      box-shadow: 0 0 0 4px rgba(255,107,112,.12);
    }}
    .bravo-signal.connected {{
      border-color: rgba(69,212,131,.66);
      background: rgba(69,212,131,.10);
      color: #b8f5d1;
    }}
    .bravo-signal.connected::before {{
      background: var(--green);
      box-shadow: 0 0 0 4px rgba(69,212,131,.14), 0 0 14px rgba(69,212,131,.42);
    }}
    .actions {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 9px;
    }}
    button {{
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #262e39;
      color: var(--text);
      font-weight: 720;
      cursor: pointer;
    }}
    button:hover {{ border-color: #526174; background: #2d3643; }}
    button.primary {{ background: #1d4f3a; border-color: #2d7a58; }}
    button.danger {{ background: #5a252a; border-color: #8c353d; }}
    button.busy {{ opacity: .62; cursor: wait; }}
    .log-panel {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .log-panel header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #171c23;
    }}
    .log-panel h2 {{
      margin: 0;
      font-size: 15px;
      font-weight: 760;
      letter-spacing: 0;
    }}
    .log-count {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .log-lines {{
      margin: 0;
      min-height: 180px;
      max-height: 320px;
      overflow: auto;
      padding: 12px 14px;
      background: #0f1216;
      color: #d4dde8;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .update-panel {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .update-panel header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #171c23;
    }}
    .update-panel h2 {{
      margin: 0;
      font-size: 15px;
      font-weight: 760;
      letter-spacing: 0;
    }}
    .update-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      padding: 14px;
      align-items: center;
    }}
    .update-row input {{
      width: 100%;
      min-width: 0;
      color: var(--text);
      border: 1px solid var(--line);
      background: #0f1216;
      border-radius: 6px;
      padding: 9px;
    }}
    .update-status {{
      min-height: 18px;
      padding: 0 14px 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    .footer {{
      margin-top: 18px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: min(360px, calc(100vw - 36px));
      background: #0f1216;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      color: var(--text);
      box-shadow: 0 14px 42px rgba(0,0,0,.35);
      opacity: 0;
      transform: translateY(8px);
      transition: opacity .2s ease, transform .2s ease;
      pointer-events: none;
    }}
    .toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 620px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .net {{ justify-content: flex-start; }}
      .signals, .actions, .update-row {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>{app}</h1>
      <div class="subline">Pico RP2040 ATX controller over W5500 Ethernet</div>
    </div>
    <div class="net">
      <span class="chip" id="link-chip">Link: checking</span>
      <span class="chip">IP: <strong>{ip}</strong></span>
      <span class="chip">{auth}</span>
    </div>
  </div>
  <main>
    <section class="grid">
{cards}
    </section>
    <section class="log-panel" aria-labelledby="log-title">
      <header>
        <h2 id="log-title">Activity Log</h2>
        <span class="log-count" id="log-count">-- entries</span>
      </header>
      <pre class="log-lines" id="log-lines">Loading...</pre>
    </section>
    <section class="update-panel" aria-labelledby="update-title">
      <header>
        <h2 id="update-title">Update main.py</h2>
        <span class="log-count">Rollback protected</span>
      </header>
      <div class="update-row">
        <input id="update-file" type="file" accept=".py,text/x-python" aria-label="Select main.py update">
        <button id="update-button" type="button">Upload and reboot</button>
      </div>
      <div class="update-status" id="update-status">No update selected.</div>
    </section>
    <div class="footer">
      <span>{version}</span>
      <span id="heap">Heap: --</span>
    </div>
  </main>
  <div class="toast" id="toast"></div>
  <script>
    const toast = document.getElementById('toast');
    function notify(message) {{
      toast.textContent = message;
      toast.classList.add('show');
      clearTimeout(window.toastTimer);
      window.toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
    }}
    async function post(route, data) {{
      const body = new URLSearchParams(data);
      const res = await fetch(route, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
        body
      }});
      if (!res.ok) throw new Error(await res.text());
      return res.text();
    }}
    async function uploadUpdate() {{
      const input = document.getElementById('update-file');
      const button = document.getElementById('update-button');
      const status = document.getElementById('update-status');
      const file = input.files && input.files[0];
      if (!file) {{
        status.textContent = 'Choose a main.py file first.';
        return;
      }}
      if (!file.name.toLowerCase().endsWith('.py')) {{
        status.textContent = 'Choose a Python .py file.';
        return;
      }}
      setBusy(button, true);
      status.textContent = 'Uploading ' + file.name + '...';
      try {{
        const res = await fetch('/update', {{
          method: 'POST',
          headers: {{'Content-Type': 'text/x-python'}},
          body: file
        }});
        const text = await res.text();
        if (!res.ok) throw new Error(text.trim() || 'Upload failed');
        status.textContent = 'Update accepted. Rebooting...';
        notify('Update installed; rebooting');
      }} catch (err) {{
        status.textContent = 'Update failed: ' + err.message;
        notify('Update failed');
        refreshLog().catch(() => {{}});
      }} finally {{
        setBusy(button, false);
      }}
    }}
    function setBusy(button, busy) {{
      if (!button) return;
      button.disabled = busy;
      button.classList.toggle('busy', busy);
    }}
    async function refreshLog() {{
      const res = await fetch('/log');
      if (!res.ok) throw new Error(await res.text());
      const text = await res.text();
      const lines = text.trim() ? text.trim().split('\\n') : [];
      const logBox = document.getElementById('log-lines');
      logBox.textContent = lines.length ? text : 'No log entries yet.';
      logBox.scrollTop = logBox.scrollHeight;
      document.getElementById('log-count').textContent = lines.length + ' entries';
    }}
    async function refresh() {{
      const res = await fetch('/status');
      const data = await res.json();
      document.getElementById('link-chip').textContent = data.link ? 'Link: online' : 'Link: offline';
      document.getElementById('heap').textContent = 'Heap: ' + (data.heap_free || '--');
      for (const pc of data.pcs) {{
        const card = document.querySelector(`[data-pc="${{pc.id}}"]`);
        if (!card) continue;
        const state = card.querySelector('[data-state]');
        const meter = card.querySelector('[data-meter]');
        const bravo = card.querySelector('[data-bravo-signal]');
        card.querySelector('[data-led]').textContent = pc.power_on ? 'ON' : 'OFF';
        card.querySelector('[data-aux]').textContent = pc.aux_on ? 'CONNECTED' : 'MISSING';
        state.textContent = pc.power_on ? 'Powered' : 'Off';
        state.classList.toggle('on', pc.power_on);
        state.classList.toggle('off', !pc.power_on);
        meter.classList.toggle('on', pc.power_on);
        meter.classList.toggle('off', !pc.power_on);
        card.classList.toggle('bravo-connected', pc.aux_on);
        bravo.classList.toggle('connected', pc.aux_on);
      }}
    }}
    document.querySelectorAll('.pc-card').forEach(card => {{
      const pc = card.dataset.pc;
      card.querySelectorAll('[data-action]').forEach(button => {{
        button.addEventListener('click', async () => {{
          const action = button.dataset.action;
          setBusy(button, true);
          try {{
            await post('/action', {{pc, action}});
            notify('Command sent');
            await refresh();
            await refreshLog();
          }} catch (err) {{
            notify('Command failed: ' + err.message);
            refreshLog().catch(() => {{}});
          }} finally {{
            setBusy(button, false);
          }}
        }});
      }});
      card.querySelector('[data-kvm]').addEventListener('click', async event => {{
        const button = event.currentTarget;
        setBusy(button, true);
        try {{
          await post('/kvm', {{port: pc}});
          notify('KVM switched');
          await refreshLog();
        }} catch (err) {{
          notify('KVM failed: ' + err.message);
          refreshLog().catch(() => {{}});
        }} finally {{
          setBusy(button, false);
        }}
      }});
      const input = card.querySelector('.label-input');
      input.addEventListener('change', async () => {{
        try {{
          await post('/label', {{pc, name: input.value}});
          notify('Label saved');
          await refreshLog();
        }} catch (err) {{
          notify('Label failed: ' + err.message);
          refreshLog().catch(() => {{}});
        }}
      }});
    }});
    document.getElementById('update-button').addEventListener('click', uploadUpdate);
    refresh();
    refreshLog().catch(() => {{}});
    setInterval(refresh, 2500);
    setInterval(() => refreshLog().catch(() => {{}}), 5000);
  </script>
</body>
</html>""".format(
        app=APP_NAME,
        ip=ip,
        auth=auth_note,
        version=APP_VERSION,
        cards="".join(cards),
    )


def handle_request(client, remote_addr=None):
    method, path_raw, headers, initial_body = read_request_start(client)
    if not method:
        return

    remote = remote_label(remote_addr)
    route, query = split_path_query(path_raw)

    if route != "/health" and not check_auth(headers):
        log("Auth failed for {} {} from {}".format(method, route, remote))
        unauthorized(client)
        return

    if method == "POST" and route == "/update":
        handle_ota_update(client, headers, initial_body, remote)
        return

    body = b""
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = read_request_body(client, headers, initial_body)
        except ValueError as exc:
            log("Invalid request body from {}: {}".format(remote, exc))
            response(client, 400, "text/plain; charset=utf-8", "{}\n".format(exc))
            return

    form = parse_kv_pairs(body.decode() if body else "")
    params = {}
    params.update(query)
    params.update(form)

    if method == "GET" and route in ("/", "/index.html"):
        log("Web UI opened from {}".format(remote))
        response(client, 200, "text/html; charset=utf-8", index_page())
    elif method == "GET" and route == "/status":
        json_response(client, status_payload())
    elif method == "GET" and route == "/health":
        json_response(client, {"ok": True, "version": APP_VERSION})
    elif method == "GET" and route == "/log":
        response(client, 200, "text/plain; charset=utf-8", log_text())
    elif method == "GET" and route == "/favicon.ico":
        response(client, 204, "image/x-icon", b"")
    elif method == "POST" and route == "/action":
        try:
            pc_id = int(params.get("pc", "0"))
            action = params.get("action", params.get("type", ""))
        except Exception as exc:
            log("Invalid action request from {}: {}".format(remote, exc))
            response(client, 400, "text/plain; charset=utf-8", "Invalid action\n")
            return
        log("Action requested from {}: pc={} action={}".format(remote, pc_id, action))
        try:
            run_action(pc_id, action)
        except ValueError as exc:
            log(
                "Action rejected from {}: pc={} action={} error={}".format(
                    remote, pc_id, action, exc
                )
            )
            response(client, 400, "text/plain; charset=utf-8", "{}\n".format(exc))
            return
        except Exception as exc:
            log(
                "Action failed from {}: pc={} action={} error={}".format(
                    remote, pc_id, action, exc
                )
            )
            raise
        log("Action completed from {}: pc={} action={}".format(remote, pc_id, action))
        json_response(client, {"ok": True})
    elif method == "POST" and route == "/kvm":
        try:
            port = int(params.get("port", "0"))
        except Exception as exc:
            log("Invalid KVM request from {}: {}".format(remote, exc))
            response(client, 400, "text/plain; charset=utf-8", "Invalid KVM port\n")
            return
        log("KVM requested from {}: port={}".format(remote, port))
        try:
            kvm_switch(port)
        except ValueError as exc:
            log("KVM rejected from {}: port={} error={}".format(remote, port, exc))
            response(client, 400, "text/plain; charset=utf-8", "{}\n".format(exc))
            return
        except Exception as exc:
            log("KVM failed from {}: port={} error={}".format(remote, port, exc))
            raise
        log("KVM completed from {}: port={}".format(remote, port))
        json_response(client, {"ok": True})
    elif method == "POST" and route in ("/label", "/setlabel"):
        try:
            pc_id = int(params.get("pc", "0"))
            name = (params.get("name", "") or "").strip()[:32]
        except Exception as exc:
            log("Invalid label request from {}: {}".format(remote, exc))
            response(client, 400, "text/plain; charset=utf-8", "Invalid label\n")
            return
        if not find_pc(pc_id) or not name:
            log("Label rejected from {}: pc={} name={}".format(remote, pc_id, name))
            response(client, 400, "text/plain; charset=utf-8", "Invalid label\n")
            return
        old_name = labels.get(pc_id, "")
        labels[pc_id] = name
        try:
            save_labels()
        except Exception as exc:
            labels[pc_id] = old_name
            log("Label save failed from {}: pc={} error={}".format(remote, pc_id, exc))
            raise
        log(
            "Label changed from {}: pc={} {} -> {}".format(
                remote, pc_id, old_name, name
            )
        )
        json_response(client, {"ok": True})
    else:
        log("Not found from {}: {} {}".format(remote, method, route))
        response(client, 404, "text/plain; charset=utf-8", "Not found\n")


def serve_http():
    feed_watchdog()
    ip, _, _, _ = get_ipv4_config(nic)
    addr = socket.getaddrinfo("0.0.0.0", HTTP_PORT)[0][-1]
    server = socket.socket()
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    server.bind(addr)
    server.listen(8)
    log("HTTP listening on http://{}:{}/".format(ip, HTTP_PORT))
    clear_ota_markers_if_healthy()
    try:
        server.settimeout(HTTP_ACCEPT_TIMEOUT_SECONDS)
    except Exception:
        pass

    while True:
        feed_watchdog()
        client = None
        try:
            client, remote_addr = server.accept()
            feed_watchdog()
            handle_request(client, remote_addr)
        except OSError as exc:
            if is_socket_timeout(exc) and client is None:
                continue
            if is_client_disconnect(exc):
                log("HTTP client disconnected")
            else:
                log("HTTP error: {}".format(exc))
                try:
                    if client:
                        response(
                            client,
                            500,
                            "text/plain; charset=utf-8",
                            "ERR: {}\n".format(exc),
                        )
                except Exception:
                    pass
        except Exception as exc:
            if is_client_disconnect(exc):
                log("HTTP client disconnected")
            else:
                log("HTTP error: {}".format(exc))
                try:
                    if client:
                        response(
                            client,
                            500,
                            "text/plain; charset=utf-8",
                            "ERR: {}\n".format(exc),
                        )
                except Exception:
                    pass
        finally:
            try:
                if client:
                    client.close()
            except Exception:
                pass
            gc.collect()


load_labels()
nic = connect_w5500_dhcp()
serve_http()
