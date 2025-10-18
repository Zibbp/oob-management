from machine import Pin, I2C, UART
import network, socket, time, ujson, os, esp32, machine, ubinascii, gc

# ========= Meta =========
APP_VERSION = "OOB-Management 1.0.0"
BUILD_TIME  = "2025-10-11"

# ========= Optional hardcoded Wi-Fi =========
FORCE_WIFI_SSID = ""           # e.g. "MySSID"
FORCE_WIFI_PASS = ""           # e.g. "supersecret"
FORCE_WIFI_OVERWRITE_NVS = True

HEARTBEAT_LED_PIN = Pin(2, Pin.OUT)
ACTIVITY_LED_PIN  = Pin(1, Pin.OUT)

# ========= NVS =========
nvs = esp32.NVS("cfg")

def get_wifi_creds():
    """Return (ssid, psk) from NVS; ('', '') on any error or if missing."""
    try:
        ssid_buf = bytearray(32)
        psk_buf  = bytearray(64)
        l1 = nvs.get_blob("wifi_ssid", ssid_buf)
        l2 = nvs.get_blob("wifi_pass", psk_buf)
        return ssid_buf[:l1].decode(), psk_buf[:l2].decode()
    except Exception:
        return "", ""

def set_wifi_creds(ssid: str, psk: str):
    """Persist Wi-Fi creds to NVS and commit."""
    nvs.set_blob("wifi_ssid", ssid.encode())
    nvs.set_blob("wifi_pass", psk.encode())
    nvs.commit()

# Push hardcoded creds if asked to.
if FORCE_WIFI_OVERWRITE_NVS and FORCE_WIFI_SSID:
    try:
        set_wifi_creds(FORCE_WIFI_SSID, FORCE_WIFI_PASS or "")
    except Exception:
        pass  # allow AP fallback

# ========= Auth =========
AUTH_NVS = esp32.NVS("auth")
DEFAULT_USER = "oob"
DEFAULT_PASS = "oob"

def get_auth_creds():
    """Return (user, pass) from NVS or defaults."""
    try:
        buf_u = bytearray(32)
        buf_p = bytearray(32)
        lu = AUTH_NVS.get_blob("user", buf_u)
        lp = AUTH_NVS.get_blob("pass", buf_p)
        user = buf_u[:lu].decode() if lu else DEFAULT_USER
        pw   = buf_p[:lp].decode() if lp else DEFAULT_PASS
        return user, pw
    except Exception:
        return DEFAULT_USER, DEFAULT_PASS

def set_auth_creds(user, pw):
    AUTH_NVS.set_blob("user", user.encode())
    AUTH_NVS.set_blob("pass", pw.encode())
    AUTH_NVS.commit()

def check_basic_auth(headers):
    """Return True if Authorization header matches stored creds."""
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        b64 = auth[6:]
        raw = ubinascii.a2b_base64(b64).decode()
        user, pw = raw.split(":", 1)
        stored_user, stored_pw = get_auth_creds()
        return user == stored_user and pw == stored_pw
    except Exception:
        return False

def send_unauth(cl):
    """Send 401 Unauthorized response."""
    try:
        cl.sendall(
            b"HTTP/1.1 401 Unauthorized\r\n"
            b"WWW-Authenticate: Basic realm=\"OOB\"\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 12\r\n\r\nUnauthorized"
        )
    except Exception:
        pass

# ==== Logging ====
LOG_BUF = []
LOG_BUF_MAX = 50

def log(msg):
    """Log message to console and buffer."""
    ts = str(time.ticks_ms())
    line = "[%s] %s" % (ts, msg)
    print(line)
    LOG_BUF.append(line)
    if len(LOG_BUF) > LOG_BUF_MAX:
        LOG_BUF.pop(0)

def get_log():
    """Return recent log lines as a string."""
    return "\n".join(LOG_BUF)

# ========= Wi-Fi bring-up & Setup AP =========
SETUP_MODE = False
ap = None
AP_ESSID = "OOB-Setup-XXXX"
AP_PASS  = "oobsetup1"  # >=8 chars

def start_ap():
    """Bring up a simple setup AP (WPA/WPA2)."""
    global ap, AP_ESSID
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    suffix = ubinascii.hexlify(machine.unique_id())[-4:].decode()
    AP_ESSID = "OOB-Setup-" + suffix
    ap.config(
        essid=AP_ESSID,
        password=AP_PASS,
        authmode=network.AUTH_WPA_WPA2_PSK,
        channel=6,
    )
    try:
        log("AP up: %s" % str(ap.ifconfig()))
    except Exception as e:
        log("AP up error: %s" % e)

def bringup_wifi():
    """Connect STA using saved creds; on missing creds enable setup AP. Return WLAN or None."""
    global SETUP_MODE
    ssid, psk = get_wifi_creds()
    if not ssid:
        SETUP_MODE = True
        start_ap()
        return None

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        try:
            log("Connecting to Wi-Fi SSID: %s" % ssid)
            wlan.connect(ssid, psk)
            # Wait ~20s for association
            for _ in range(80):
                if wlan.isconnected():
                    break
                time.sleep(0.25)
        except Exception as e:
            log("Wi-Fi connect error: %s" % e)

    log("IP: %s" % (wlan.ifconfig()[0] if wlan.isconnected() else "No WiFi"))
    return wlan

wlan = bringup_wifi()

# ========= Small utils =========
def _mac_str(b):
    """Return MAC bytes as 'AA:BB:CC:DD:EE:FF' string; 'NA' on error."""
    try:
        return ":".join("{:02X}".format(x) for x in b)
    except Exception:
        return "NA"

def fs_stats_mb():
    """Return (total_mb, free_mb) for '/' FS; (None, None) on error."""
    try:
        s = os.statvfs("/")
        bs = s[0]
        total = (s[2] * bs) / (1024 * 1024)
        free  = (s[3] * bs) / (1024 * 1024)
        return round(total, 1), round(free, 1)
    except Exception:
        return None, None

def get_hostname():
    """Return configured hostname if available; else NVS 'hostname' or ''."""
    try:
        return network.hostname()
    except Exception:
        try:
            return nvs.get_str("hostname")
        except Exception:
            return ""

def wifi_rssi(w):
    """Return RSSI from WLAN or None if not available."""
    try:
        return w.status("rssi")
    except Exception:
        return None

def sys_info(wlan_obj=None, ap_obj=None):
    """Collect basic system, FS, and Wi-Fi/AP details into a dict."""
    total_mb, free_mb = fs_stats_mb()
    info = {
        "version": APP_VERSION,
        "build_time": BUILD_TIME,
        "unique_id": ubinascii.hexlify(machine.unique_id()).decode(),
        "reset_cause": machine.reset_cause(),
        "heap_free": gc.mem_free(),
        "heap_alloc": gc.mem_alloc() if hasattr(gc, "mem_alloc") else None,
        "fs_total_mb": total_mb,
        "fs_free_mb": free_mb,
        "hostname": get_hostname(),
        "setup_mode": SETUP_MODE,
        "sta": {},
        "ap": {},
    }

    # STA info
    try:
        w = wlan_obj or network.WLAN(network.STA_IF)
        if w.active():
            ip, mask, gw, dns = w.ifconfig()
            info["sta"] = {
                "active": True,
                "connected": w.isconnected(),
                "ip": ip, "mask": mask, "gw": gw, "dns": dns,
                "mac": _mac_str(w.config("mac")),
                "rssi": wifi_rssi(w),
            }
    except Exception:
        pass

    # AP info
    try:
        a = ap_obj or network.WLAN(network.AP_IF)
        if a.active():
            ip, mask, gw, dns = a.ifconfig()
            try:
                ch = a.config("channel")
            except Exception:
                ch = None
            info["ap"] = {
                "active": True,
                "ip": ip, "mask": mask, "gw": gw, "dns": dns,
                "mac": _mac_str(a.config("mac")),
                "channel": ch,
                "essid": a.config("essid") if hasattr(a, "config") else None,
            }
    except Exception:
        pass

    return info

def print_boot_banner(wlan_obj=None, ap_obj=None):
    """Pretty-print boot/system information to the serial console."""
    si = sys_info(wlan_obj, ap_obj)
    log("=== OOB Controller Boot ===")
    log("Version: %s Build: %s" % (si["version"], si["build_time"]))
    log("UID: %s ResetCause: %s" % (si["unique_id"], si["reset_cause"]))
    log("Hostname: %s SetupMode: %s" % (si["hostname"], si["setup_mode"]))
    log("Heap free: %s alloc: %s" % (si["heap_free"], si["heap_alloc"]))
    log("FS total/free (MB): %s / %s" % (si["fs_total_mb"], si["fs_free_mb"]))
    sta = si.get("sta", {})
    log("STA: %s %s" % ("active" if sta.get("active") else "inactive",
                        "connected" if sta.get("connected") else "disconnected"))
    if sta.get("active"):
        log("  IP: %s GW: %s Mask: %s DNS: %s" % (sta.get("ip"), sta.get("gw"), sta.get("mask"), sta.get("dns")))
        log("  MAC: %s RSSI: %s" % (sta.get("mac"), sta.get("rssi")))
    apd = si.get("ap", {})
    log("AP: %s" % ("active" if apd.get("active") else "inactive"))
    if apd.get("active"):
        log("  ESSID: %s Chan: %s" % (apd.get("essid"), apd.get("channel")))
        log("  IP: %s MAC: %s" % (apd.get("ip"), apd.get("mac")))
    log("=== Boot complete ===")

print_boot_banner(wlan, ap)

def heartbeat_led_task():
    """Blink heartbeat LED: slow when online, fast when offline."""
    while True:
        HEARTBEAT_LED_PIN.on()
        time.sleep(0.1)
        HEARTBEAT_LED_PIN.off()
        time.sleep(1.9 if (wlan and wlan.isconnected()) else 0.1)

import _thread
_thread.start_new_thread(heartbeat_led_task, ())

# ========= Hardware (MCP23017 + UART KVM) =========
MCP23017_ADDR = 0x20
IODIRA, IODIRB = 0x00, 0x01
OLATA,  OLATB  = 0x14, 0x15
GPIOA,  GPIOB  = 0x12, 0x13
GPPUA,  GPPUB  = 0x0C, 0x0D

i2c = I2C(0, scl=Pin(9), sda=Pin(8), freq=400000)
kvm_uart = UART(1, baudrate=19200, tx=Pin(5), rx=Pin(4))

def kvm_switch(port: int):
    """Send KVM switch command for ports 1..4 via UART."""
    log("KVM switch: port=%d" % port)
    if 1 <= port <= 4:
        kvm_uart.write("G0{}gA".format(port))

# Set all A/B pins to output initially (we’ll flip to input on reads).
i2c.writeto_mem(MCP23017_ADDR, IODIRA, b"\x00")
i2c.writeto_mem(MCP23017_ADDR, IODIRB, b"\x00")

def pulse(port: str, bit: int, seconds=1):
    """Drive OLAT 'bit' high for 'seconds' then low (one-shot)."""
    reg = OLATA if port == "A" else OLATB
    cur = i2c.readfrom_mem(MCP23017_ADDR, reg, 1)[0]
    i2c.writeto_mem(MCP23017_ADDR, reg, bytes([cur | (1 << bit)]))
    time.sleep(seconds)
    cur = i2c.readfrom_mem(MCP23017_ADDR, reg, 1)[0]
    i2c.writeto_mem(MCP23017_ADDR, reg, bytes([cur & ~(1 << bit)]))

def read_input(port: str, bit: int) -> int:
    """Configure pin as input with pull-up and return its logic level (0/1)."""
    iodir = IODIRA if port == "A" else IODIRB
    gppu  = GPPUA if port == "A" else GPPUB
    gpio  = GPIOA if port == "A" else GPIOB

    # Input direction for this bit
    val = i2c.readfrom_mem(MCP23017_ADDR, iodir, 1)[0]
    i2c.writeto_mem(MCP23017_ADDR, iodir, bytes([val | (1 << bit)]))
    # Enable pull-up
    val = i2c.readfrom_mem(MCP23017_ADDR, gppu, 1)[0]
    i2c.writeto_mem(MCP23017_ADDR, gppu, bytes([val | (1 << bit)]))
    # Read value
    v = i2c.readfrom_mem(MCP23017_ADDR, gpio, 1)[0]
    return (v >> bit) & 1

def _fsync():
    """Best-effort filesystem sync (no-op if unsupported)."""
    try:
        os.sync()
    except AttributeError:
        pass

# ========= Simple URL & HTTP helpers =========
def url_unquote(s: str) -> str:
    """Decode minimal 'application/x-www-form-urlencoded' string."""
    res, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "%" and i + 2 < len(s):
            try:
                res.append(chr(int(s[i+1:i+3], 16)))
                i += 3
                continue
            except Exception:
                pass
        res.append(" " if c == "+" else c)
        i += 1
    return "".join(res)

def parse_kv_pairs(s: str) -> dict:
    """Parse 'k=v&...' form data into a dict with URL-decoded keys/values."""
    out = {}
    if not s:
        return out
    for kv in s.split("&"):
        if not kv:
            continue
        k, v = (kv.split("=", 1) + [""])[:2]
        out[url_unquote(k)] = url_unquote(v)
    return out

def split_path_query(path: str):
    """Return (route, query_dict) from a path with optional '?q=...'."""
    if "?" in path:
        route, qs = path.split("?", 1)
        return route, parse_kv_pairs(qs)
    return path, {}

def read_headers_and_rest(cl):
    """Read request line+headers and return (method, path, headers, rest_bytes)."""
    cl.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = cl.recv(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > 64 * 1024:  # guard against header abuse
            break
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        return "", "", {}, b""
    try:
        method, path, _proto = lines[0].decode().split(" ", 2)
    except Exception:
        return "", "", {}, b""
    headers = {}
    for ln in lines[1:]:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            headers[k.decode().strip().lower()] = v.decode().strip()
    return method, path, headers, rest

def send_resp(cl, code=200, ctype="text/plain; charset=utf-8", body=b"OK"):
    """Send a minimal HTTP/1.1 response with Content-Length."""
    try:
        cl.sendall(
            b"HTTP/1.1 " + str(code).encode() + b" " +
            (b"OK" if code == 200 else b"ERR") + b"\r\n" +
            b"Content-Type: " + ctype.encode() + b"\r\n" +
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        if body:
            cl.sendall(body)
    except Exception:
        pass

# ========= Labels (NVS) =========
nvs_labels = esp32.NVS("labels")
LABELS = {1: "PC 1", 2: "PC 2", 3: "PC 3", 4: "PC 4"}

def save_labels():
    """Persist LABELS dict to NVS and commit."""
    log("Saving labels: %s" % str(LABELS))
    for pc, name in LABELS.items():
        nvs_labels.set_blob("pc{}".format(pc), name.encode())
    nvs_labels.commit()

def load_labels():
    """Load LABELS from NVS (keeps defaults for missing)."""
    global LABELS
    for pc in LABELS.keys():
        try:
            buf = bytearray(32)
            l = nvs_labels.get_blob("pc{}".format(pc), buf)
            if l and l > 0:
                LABELS[pc] = buf[:l].decode()
        except OSError:
            pass

load_labels()

# ========= PC mapping =========
PCS = {
    1: {"port": "B", "power": 0, "reset": 1, "led": 2, "bravo": 3},
    2: {"port": "B", "power": 4, "reset": 5, "led": 6, "bravo": 7},
    3: {"port": "A", "power": 0, "reset": 1, "led": 2, "bravo": 3},
    4: {"port": "A", "power": 4, "reset": 5, "led": 6, "bravo": 7},
}

def pc_status():
    """Return dict of per-PC LED/Bravo indicators and label."""
    out = {}
    for pc in sorted(PCS.keys()):
        m = PCS[pc]
        # Active-low sensors -> map to green square when 0
        led_raw   = 0 if read_input(m["port"], m["led"])   == 0 else 1
        bravo_raw = 0 if read_input(m["port"], m["bravo"]) == 0 else 1
        out[pc] = {
            "led":   "🟩" if led_raw   == 0 else "⬛",
            "bravo": "🟩" if bravo_raw == 0 else "⬛",
            "label": LABELS[pc],
        }
    return out

def setup_page():
    """Return HTML for Wi-Fi setup when in setup mode."""
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:sans-serif;max-width:520px;margin:24px auto;padding:0 12px}
.card{border:1px solid #ddd;border-radius:10px;padding:16px;box-shadow:0 2px 6px rgba(0,0,0,.05)}
h1{font-size:20px;margin:0 0 12px}label{display:block;margin:.6rem 0 .2rem}</style>
<div class="card">
<h1>Wi-Fi setup</h1>
<p>Connect this device to your Wi-Fi network. It will reboot and exit setup mode.</p>
<form method="POST" action="/setwifi">
  <label>SSID</label>
  <input name="ssid" autofocus>
  <label>Password</label>
  <input type="password" name="psk">
  <p><button type="submit">Save & Reboot</button></p>
</form>
<p style="font-size:12px;color:#666">AP SSID: %s &nbsp; Password: %s<br>Version: %s</p>
</div>""" % (AP_ESSID, AP_PASS, APP_VERSION)

def render_main_page():
    """Load index.html and inject PC cards and Wi-Fi placeholders."""
    if SETUP_MODE:
        return setup_page()

    cur_ssid, cur_psk = get_wifi_creds()
    try:
        with open("index.html") as f:
            html = f.read()
    except Exception:
        html = """<html><body>Error loading index.html</body></html>"""

    cards = []
    for pc in sorted(PCS.keys()):
        lbl = LABELS[pc]
        cards.append("""
        <div class="pc-card">
          <div class="pc-title">
            <input id="label%d" value="%s" onchange="updateLabel(%d)">
          </div>
          <div class="status">Power LED: <span id="led%d">?</span></div>
          <div class="status">Bravo Conn: <span id="bravo%d">?</span></div>
          <button class="btn-on"  data-label="%s" onclick="doAction(%d,'poweron',this)">Power On</button>
          <button class="btn-off" data-label="%s" onclick="doAction(%d,'poweroff',this)">Power Off</button>
          <button class="btn-rst" data-label="%s" onclick="doAction(%d,'reset',this)">Reset</button>
          <hr><button onclick="kvmAction(%d)">Switch KVM</button>
        </div>
        """ % (pc, lbl, pc, pc, pc, lbl, pc, lbl, pc, lbl, pc, pc))
    html = html.replace("##PC_CARDS##", "".join(cards))
    html = html.replace("##SSID##", cur_ssid)
    html = html.replace("##PSK##", cur_psk)
    html = html.replace("##VERSION##", APP_VERSION)
    return html

# ========= HTTP server =========
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 80))
srv.listen(1)

while True:
    cl, addr = srv.accept()
    # uncomment to log every connection
    # log("Connection from %s" % str(addr))

    # Brief activity LED pulse per connection
    ACTIVITY_LED_PIN.on()
    time.sleep(0.05)
    ACTIVITY_LED_PIN.off()

    cl.settimeout(15)
    try:
        method, path_raw, headers, rest = read_headers_and_rest(cl)
        # uncomment to log every request
        # log("Request: %s %s" % (method, path_raw))
        route, query = split_path_query(path_raw)

        # Body length & body read (if present)
        clen = int(headers.get("content-length", "0") or "0")
        body = rest + (cl.recv(clen - len(rest)) if clen > len(rest) else b"")
        form = parse_kv_pairs(body.decode() if body else "")

        # Merge params: form overrides query
        params = {}
        params.update(query)
        params.update(form)

        # Allow unauthenticated access only to /setwifi in setup mode
        if not (SETUP_MODE and route == "/setwifi"):
            if not check_basic_auth(headers):
                send_unauth(cl)
                cl.close()
                continue

        if method == "GET" and route == "/":
            page = render_main_page().encode()
            send_resp(cl, 200, "text/html; charset=utf-8", page)

        elif method == "GET" and route == "/status":
            send_resp(cl, 200, "application/json", ujson.dumps(pc_status()).encode())

        elif route == "/favicon.ico":
            # Avoid noisy 404s
            send_resp(cl, 200, "image/x-icon", b"")

        elif method == "GET" and route == "/log":
            send_resp(cl, 200, "text/plain; charset=utf-8", get_log().encode())

        elif method == "GET" and route == "/health":
            send_resp(cl, 200, "application/json", ujson.dumps({"ok": True}).encode())

        elif method == "POST" and route == "/action":
            try:
                pc  = int(params.get("pc", "0"))
                typ = params.get("type", "")
                log("Action: pc=%d type=%s" % (pc, typ))
                p = PCS.get(pc)
                if p:
                    if   typ == "poweron":  pulse(p["port"], p["power"], 1)
                    elif typ == "poweroff": pulse(p["port"], p["power"], 5)
                    elif typ == "reset":    pulse(p["port"], p["reset"], 1)
                send_resp(cl, 200, "text/plain; charset=utf-8", b"OK")
            except Exception as e:
                log("Action error: %s" % e)
                send_resp(cl, 500, "text/plain; charset=utf-8", ("ERR: %s" % e).encode())

        elif method == "POST" and route == "/setlabel":
            try:
                pc   = int(params.get("pc", "0"))
                name = params.get("name", "")
                log("Set label: pc=%d name=%s" % (pc, name))
                if pc in LABELS and name:
                    LABELS[pc] = name
                    save_labels()
                send_resp(cl, 200, "text/plain; charset=utf-8", b"OK")
            except Exception as e:
                log("Set label error: %s" % e)
                send_resp(cl, 500, "text/plain; charset=utf-8", ("ERR: %s" % e).encode())

        elif method == "POST" and route == "/kvm":
            try:
                port = int(params.get("port", "0"))
                log("KVM POST: port=%d" % port)
                kvm_switch(port)
                send_resp(cl, 200, "text/plain; charset=utf-8", b"OK")
            except Exception as e:
                log("KVM error: %s" % e)
                send_resp(cl, 500, "text/plain; charset=utf-8", ("ERR: %s" % e).encode())

        elif method == "POST" and route == "/setwifi":
            ssid = params.get("ssid", "")
            psk  = params.get("psk", "")
            log("Set Wi-Fi: ssid=%s psk=%s" % (ssid, "***" if psk else "(empty)"))
            if ssid:
                set_wifi_creds(ssid, psk or "")
                send_resp(cl, 200, "text/plain; charset=utf-8", b"Saved! Rebooting...")
                try:
                    cl.close()
                except Exception:
                    pass
                _fsync()
                time.sleep(1.5)
                log("Rebooting...")
                machine.reset()
            else:
                send_resp(cl, 400, "text/plain; charset=utf-8", b"Missing SSID")

        elif method == "POST" and route == "/setauth":
            user = params.get("user", "")
            pw   = params.get("pass", "")
            if user and pw:
                set_auth_creds(user, pw)
                send_resp(cl, 200, "text/plain; charset=utf-8", b"Auth updated! Rebooting...")
                try: cl.close()
                except: pass
                _fsync()
                time.sleep(1.5)
                log("Rebooting for auth update...")
                machine.reset()
            else:
                send_resp(cl, 400, "text/plain; charset=utf-8", b"Missing user/pass")

        else:
            # Uncomment to log unknown routes
            # log("Unknown route: %s %s" % (method, route))
            send_resp(cl, 404, "text/plain; charset=utf-8", b"Not found")

    except Exception as e:
        # Uncomment to log all request handling errors
        # log("Request handling error: %s" % e)
        try:
            send_resp(cl, 500, "text/plain; charset=utf-8", ("ERR: %s" % e).encode())
        except Exception:
            pass
    finally:
        try:
            cl.close()
        except Exception:
            pass
        gc.collect()
