#!/usr/bin/env python3
"""NetBridge status dashboard - stdlib only.
Serves:
  GET /             HTML dashboard (auto-refresh)
  GET /api/status   full status JSON (also consumed by the fleet agent / control plane)
  GET /api/checks   presenter green checks (samples ~2s; kept out of gather() so
                    status/telemetry stay instant)
  GET /api/health   tiny liveness JSON
on http://<pi>:8080"""
import http.server, socketserver, subprocess, os, time, socket, json, hashlib

PORT = 8080
VERSION_FILE = "/etc/bridge/version"
SERVICES = ["bridge-gadget", "bridge-feeder-net", "bridge-uvcd",
            "bridge-feeder-audio", "bridge-return-audio"]

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""

def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""

def wifi_dbm():
    try:
        lines = read("/proc/net/wireless").splitlines()
        for ln in lines[2:]:
            parts = ln.split()
            if parts:
                return parts[3].rstrip(".")
    except Exception:
        pass
    return ""

def cpu_serial():
    # Stable per-Pi hardware id. Falls back to the machine-id, then hostname.
    for ln in read("/proc/cpuinfo").splitlines():
        if ln.lower().startswith("serial"):
            v = ln.split(":", 1)[1].strip()
            if v and set(v) != {"0"}:
                return v
    mid = read("/etc/machine-id")
    return mid or sh("hostname")

def pairing_code(serial):
    # Human-friendly claim code printed on a sticker / shown below. Deterministic
    # from the hardware id so it survives reflash: BRIDGE-XXXX (hex).
    h = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:4].upper()
    return "BRIDGE-" + h

def tailscale_ip4():
    return sh("tailscale ip -4 2>/dev/null").splitlines()[0] if sh("tailscale ip -4 2>/dev/null") else ""

def svc_restarts(svc):
    v = sh("systemctl show -p NRestarts --value %s" % svc)
    try:
        return int(v)
    except Exception:
        return 0

def clock_suspect():
    # Heuristic flag for a degrading UAC2 gadget clock (the cause of crackly return
    # audio). True FFT detection is deferred; here we look for the I/O-error cascade
    # in recent return-audio logs. The fix remains `bridge reset-clock`.
    errs = sh("journalctl -u bridge-return-audio --since '-10 min' 2>/dev/null "
              "| grep -ci 'input/output error'")
    try:
        return int(errs) > 0
    except Exception:
        return False

def gather():
    d = {}
    d["host"] = sh("hostname")
    hi = sh("hostname -I")
    d["ip"] = hi.split()[0] if hi else "?"
    d["services"] = [(s, sh("systemctl is-active %s" % s)) for s in SERVICES]
    state = ""
    udcdir = "/sys/class/udc"
    if os.path.isdir(udcdir):
        for f in os.listdir(udcdir):
            state = read("%s/%s/state" % (udcdir, f))
            d["speed"] = read("%s/%s/current_speed" % (udcdir, f))
            break
    d["udc"] = state or "?"
    d["functions"] = sh("ls /sys/kernel/config/usb_gadget/g1/functions/ 2>/dev/null").replace("\n", " ")
    d["video40"] = os.path.exists("/dev/video40")
    d["uac2"] = os.path.isdir("/proc/asound/UAC2Gadget")
    d["temp"] = sh("vcgencmd measure_temp").replace("temp=", "")
    d["throttled"] = sh("vcgencmd get_throttled").replace("throttled=", "")
    d["wifi"] = wifi_dbm()
    d["uptime"] = sh("uptime -p").replace("up ", "")
    peer = ""
    for ln in read("/etc/default/bridge-return-audio").splitlines():
        if ln.startswith("RETURN_DEST_IP"):
            peer = ln.split("=", 1)[1]
        if ln.startswith("RETURN_DEST_PORT"):
            peer += ":" + ln.split("=", 1)[1]
    d["peer"] = peer or "?"
    d["wd_timer"] = sh("systemctl is-active bridge-watchdog.timer")
    d["wd_hw"] = sh("systemctl show -p RuntimeWatchdogUSec --value")
    # --- fleet identity + telemetry (consumed by the control plane) ---
    serial = cpu_serial()
    d["device_id"] = serial
    d["pairing_code"] = pairing_code(serial)
    d["version"] = read(VERSION_FILE) or "dev"
    d["tailscale_ip"] = tailscale_ip4()
    svc = dict(d["services"])
    # Per-stream up/down. Service "active" is the cheap proxy; a configured UDC means a
    # client is actually attached so the forward streams can land somewhere.
    attached = d["udc"] == "configured"
    d["streams"] = {
        "video":  svc.get("bridge-feeder-net") == "active" and attached,
        "voice":  svc.get("bridge-feeder-audio") == "active" and attached,
        "return": svc.get("bridge-return-audio") == "active" and attached,
    }
    d["restarts"] = {
        "feeder_net": svc_restarts("bridge-feeder-net"),
        "uvcd": svc_restarts("bridge-uvcd"),
        "return_audio": svc_restarts("bridge-return-audio"),
    }
    d["clock_suspect"] = clock_suspect()
    d["ts"] = int(time.time())
    return d

# --------------------------- presenter green checks ---------------------------
# Journey 3: four am-I-live booleans a presenter can trust. These sample real
# activity (CPU-tick and hw_ptr deltas), not lifetime averages (`ps pcpu` lies),
# so they block ~2s. That is why they live on /api/checks, NOT in gather().

PCM_RETURN_STATUS = "/proc/asound/UAC2Gadget/pcm0c/sub0/status"

def _udc_state():
    udcdir = "/sys/class/udc"
    if os.path.isdir(udcdir):
        for f in os.listdir(udcdir):
            return read("%s/%s/state" % (udcdir, f))
    return ""

def _feeder_cpu_ticks():
    """(pid, utime+stime clock ticks) of the net video feeder, or (None, None)."""
    pids = sh("pgrep -f 'udpsrc port=5000'").split()
    if not pids:
        return None, None
    stat = read("/proc/%s/stat" % pids[0])
    try:
        f = stat.rsplit(")", 1)[1].split()   # fields after comm; utime/stime = 14/15 1-indexed
        return pids[0], int(f[11]) + int(f[12])
    except Exception:
        return pids[0], None

def _return_hw_ptr():
    """Capture-side (from client) hw_ptr in frames, or None if stream closed."""
    st = read(PCM_RETURN_STATUS)
    if not st or st.startswith("closed"):
        return None
    for ln in st.splitlines():
        if ln.startswith("hw_ptr"):
            try:
                return int(ln.split(":", 1)[1].split()[0])
            except Exception:
                return None
    return None

def checks():
    win = 2.0                       # one shared sampling window for both deltas
    pid, t0 = _feeder_cpu_ticks()
    p0 = _return_hw_ptr()
    time.sleep(win)
    _, t1 = _feeder_cpu_ticks()
    p1 = _return_hw_ptr()

    if pid is None:
        video_ok, video_detail = False, "video feeder process not running"
    elif t0 is None or t1 is None:
        video_ok, video_detail = False, "feeder pid %s: could not read /proc stat" % pid
    else:
        dt = t1 - t0
        video_ok = dt > 10          # >10 cpu ticks in 2s = actively decoding RTP
        video_detail = "feeder pid %s used %d cpu ticks in %.0fs" % (pid, dt, win)

    if p0 is None or p1 is None:
        audio_ok, audio_detail = False, "return capture stream not open (client mic path idle)"
    else:
        dp = p1 - p0
        audio_ok = dp > 40000 * win  # 48 kHz nominal; >40k frames/s = flowing
        audio_detail = "hw_ptr advanced %d frames in %.0fs (~%d/s)" % (dp, win, dp / win)

    udc = _udc_state()
    return {
        "online": {"ok": True, "detail": "bridge-web serving on :%d" % PORT},
        "video_arriving": {"ok": video_ok, "detail": video_detail},
        "client_sees_camera": {"ok": udc == "configured",
                               "detail": "usb gadget state: %s" % (udc or "?")},
        "return_audio": {"ok": audio_ok, "detail": audio_detail},
        "ts": int(time.time()),
    }

def badge(ok, text):
    color = "#1a7f37" if ok else "#b42318"
    return '<span style="background:%s;color:#fff;padding:2px 9px;border-radius:10px;font-size:13px">%s</span>' % (color, text)

def page():
    d = gather()
    rows = ""
    for s, st in d["services"]:
        rows += "<tr><td>%s</td><td>%s</td></tr>" % (s, badge(st == "active", st))
    usb_ok = d["udc"] == "configured"
    html = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=3>
<title>NetBridge - %(host)s</title>
<style>body{font-family:system-ui,Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:18px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#8b949e;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%%;max-width:560px;margin-bottom:18px}
td{padding:7px 10px;border-bottom:1px solid #21262d;font-size:15px}
td:first-child{color:#8b949e}.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px 16px;max-width:560px;margin-bottom:14px}
.k{color:#8b949e}</style></head><body>
<h1>NetBridge &middot; %(host)s</h1>
<div class=sub>%(ip)s &nbsp;|&nbsp; updated %(now)s &nbsp;|&nbsp; auto-refresh 3s</div>
<div class=card><b>Services</b><table>%(rows)s</table></div>
<div class=card><b>USB client</b><br>
host: %(usb)s &nbsp; speed: %(speed)s<br>
functions: %(functions)s</div>
<div class=card><b>Devices</b><br>
loopback /dev/video40: %(v40)s &nbsp; UAC2 card: %(uac2)s &nbsp; return peer: %(peer)s</div>
<div class=card><b>Health</b><br>
temp: %(temp)s &nbsp; throttled: %(thr)s &nbsp; wifi: %(wifi)s dBm<br>
uptime: %(uptime)s &nbsp; watchdog: timer=%(wdt)s hw=%(wdh)s</div>
<div class=card><b>Fleet</b><br>
pairing code: <b>%(pair)s</b> &nbsp; version: %(ver)s<br>
tailscale: %(tsip)s &nbsp; clock: %(clock)s<br>
<span class=k>device id: %(devid)s</span></div>
</body></html>""" % {
        "host": d["host"], "ip": d["ip"], "now": time.strftime("%H:%M:%S"),
        "rows": rows, "usb": badge(usb_ok, d["udc"] + (" (no client)" if not usb_ok else "")),
        "speed": d.get("speed", "?"), "functions": d["functions"] or "-",
        "v40": badge(d["video40"], "ok" if d["video40"] else "missing"),
        "uac2": badge(d["uac2"], "ok" if d["uac2"] else "missing"),
        "peer": d["peer"], "temp": d["temp"], "thr": d["throttled"],
        "wifi": d["wifi"] or "-", "uptime": d["uptime"],
        "wdt": d["wd_timer"], "wdh": d["wd_hw"],
        "pair": d["pairing_code"], "ver": d["version"],
        "tsip": d["tailscale_ip"] or "-",
        "clock": badge(not d["clock_suspect"], "ok" if not d["clock_suspect"] else "suspect"),
        "devid": d["device_id"],
    }
    return html

class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/status":
            self._send(json.dumps(gather()).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/checks":
            self._send(json.dumps(checks()).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/health":
            self._send(json.dumps({"ok": True, "ts": int(time.time())}).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(page().encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    address_family = socket.AF_INET6
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()

if __name__ == "__main__":
    Server(("::", PORT), H).serve_forever()
