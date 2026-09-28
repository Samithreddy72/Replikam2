#!/usr/bin/env python3
"""The presenter app's go-live, end to end, on BOTH platform paths (2026-09-25).

The owner went live without typing a PIN. This runs the app's real local server
(app/netbridge-source/source_app.py) the way the page and the Studio app call it, with the fleet
simulated and the BRIDGE being the real pi/scripts/bridge-web.py request handler on top of the
real pi/scripts/bridge-pin (throwaway state, fake nft). Nothing touches a camera, a network or a
real bridge. Every scenario runs twice: once as macOS, once as Windows (IS_WIN) - the PIN path
must not depend on the platform.

  python3 tests/test-app-pin-flow.py
"""
import importlib.util, io, json, os, pathlib, subprocess, sys, tempfile, threading, types
import urllib.request, urllib.error
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
APPDIR = ROOT / "app" / "netbridge-source"
WEB = ROOT / "pi" / "scripts" / "bridge-web.py"
TOOL = ROOT / "pi" / "scripts" / "bridge-pin"
sys.path.insert(0, str(APPDIR))
import source_app as app                                    # noqa: E402

T = pathlib.Path(tempfile.mkdtemp())
ETC, RUN, BIN, LOGS = T / "etc", T / "run", T / "bin", T / "logs"
for d in (ETC, RUN, BIN, LOGS):
    d.mkdir()
(BIN / "nft").write_text(r'''#!/usr/bin/env python3
import json, re, sys
S = %r
try: st = json.load(open(S))
except Exception: st = {"exists": False, "peer4": []}
a = sys.argv[1:]
if a[:2] == ["-f", "-"]:
    sc = sys.stdin.read()
    if "delete table inet netbridge_gate" in sc:
        m = re.search(r"set peer4 \{[^}]*?elements = \{ ([^}]*) \}", sc)
        st = {"exists": True, "peer4": [m.group(1).strip()] if m else []}
    elif not st.get("exists"):
        sys.exit(1)
    else:
        for l in sc.splitlines():
            if l.strip() == "flush set inet netbridge_gate peer4": st["peer4"] = []
            m = re.match(r"add element inet netbridge_gate peer4 \{ (.+) \}$", l.strip())
            if m: st["peer4"].append(m.group(1))
    json.dump(st, open(S, "w")); sys.exit(0)
if a[:2] == ["list", "table"] and st.get("exists"):
    print("table inet netbridge_gate {\n\tcounter refused_in {\n\t}\n\tset peer4 {\n" +
          ("\t\telements = { %%s }\n" %% ", ".join(st["peer4"]) if st["peer4"] else "") +
          "\t}\n\tchain media_in {\n\t}\n}"); sys.exit(0)
if a[:2] == ["list", "counter"] and st.get("exists"):
    print("counter video_in { packets 0 bytes 0 }"); sys.exit(0)
sys.exit(1)
''' % str(T / "nft.json"))
(BIN / "logger").write_text("#!/bin/sh\nexit 0\n")
for f in BIN.iterdir():
    f.chmod(0o755)
PINENV = dict(os.environ, BRIDGE_PIN_ETC=str(ETC), BRIDGE_PIN_RUN=str(RUN),
              BRIDGE_PIN_NFT=str(BIN / "nft"), PATH="%s:%s" % (BIN, os.environ["PATH"]))
REAL_RUN = subprocess.run

def tool(*args, stdin=None):
    p = REAL_RUN([sys.executable, str(TOOL)] + list(args), input=stdin, capture_output=True,
                 text=True, env=PINENV)
    return p.returncode, p.stdout

# ---- the bridge: bridge-web's real handler, bridge-pin underneath -----------------------------
spec = importlib.util.spec_from_file_location("bw", WEB)
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)
bw.PIN_STATE_FILE = str(RUN / "state.json")
BRIDGE_LOG = []                 # (path, body) of every request the bridge received
def bw_run(argv, *a, **kw):
    if isinstance(argv, (list, tuple)) and list(argv[:3]) == ["sudo", "-n", bw.PIN_TOOL]:
        return REAL_RUN([sys.executable, str(TOOL)] + list(argv[3:]), *a, env=PINENV, **kw)
    return subprocess.CompletedProcess(argv, 0, "", "")
bw.subprocess = types.SimpleNamespace(run=bw_run, CompletedProcess=subprocess.CompletedProcess,
                                      SubprocessError=subprocess.SubprocessError)
bw.read = lambda path: ""
OLD_BRIDGE = {"on": False}
LOCKSTATE_FAILS = {"n": 0}

def bridge(method, path, body, caller):
    BRIDGE_LOG.append((method, path, json.loads(json.dumps(body or {}))))
    if path == "/api/lock-state" and LOCKSTATE_FAILS["n"] > 0:
        LOCKSTATE_FAILS["n"] -= 1
        return {"_error": "timed out"}
    if OLD_BRIDGE["on"] and path == "/api/lock-state":
        return {"pin_set": True, "locked": True, "lockout": False, "lockout_remaining": 0}
    if path == "/api/checks":
        return {"pin": {"locked": not (bw.pin_state().get("session") or {}).get("active"),
                        "protocol": 2, "last_end": (bw.pin_state().get("last_end") or {}).get("reason")}}
    h = bw.H.__new__(bw.H)
    raw = json.dumps(body or {}).encode()
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.headers = {"Content-Length": str(len(raw))}
    h.path, h.command, h.request_version = path, method, "HTTP/1.1"
    h.requestline = "%s %s HTTP/1.1" % (method, path)
    h.client_address = ("::ffff:" + caller, 40000)
    (h.do_POST if method == "POST" else h.do_GET)()
    out = h.wfile.getvalue().decode()
    status = int(out.split(" ", 2)[1])
    j = json.loads(out.split("\r\n\r\n", 1)[1])
    if status >= 400:        # what app.api() returns for an HTTP error
        e = {"_error": j.get("detail", "HTTP %d" % status), "_code": status}
        if j.get("reason"):
            e["_reason"] = j["reason"]
        return e
    return j

# ---- the fleet + the mesh helper ---------------------------------------------------------------
CONTROL = "https://fleet.test"
HELPER = {"ip": "100.90.0.1", "n": 1, "alive": False}
FLEET_LOG = []
AUTH_BRIDGES = {"exists": True}
BRIDGE_REC = {"id": "10000000aaaa0001", "number": 1, "label": "NB-001", "name": "Studio A",
              "pairing_code": "BRIDGE-0001", "online": True, "tailscale_ip": "100.64.0.10",
              "ip": "192.168.1.50"}

def fake_api(method, url, token=None, body=None, timeout=10):
    if url.startswith(CONTROL):
        FLEET_LOG.append((method, url, json.dumps(body or {})))
        path = url[len(CONTROL):]
        if path == "/auth/bridges":
            return [dict(BRIDGE_REC)] if AUTH_BRIDGES["exists"] else {"_error": "Not Found", "_code": 404}
        if path == "/admin/devices":
            d = {k: v for k, v in BRIDGE_REC.items() if k != "ip"}
            d.update(latest={"ip": "192.168.1.50"}, claimed=True)
            return [d]
        return {"_error": "unexpected fleet call %s" % path}
    assert url.startswith("http://127.0.0.1:18080"), url
    return bridge(method, url[len("http://127.0.0.1:18080"):], body, HELPER["ip"])

def fake_route(rec, st):
    if not HELPER["alive"]:
        HELPER["alive"] = True
    app.MESH.bridge_id = rec.get("id")
    app.MESH.control_port = 18080
    app.MESH.tailnet_ip = HELPER["ip"]
    app.MESH.proc = types.SimpleNamespace(poll=lambda: None if HELPER["alive"] else 0,
                                          pid=4242, terminate=lambda: None, wait=lambda timeout=None: 0,
                                          kill=lambda: None)
    return {"via": "mesh", "control_host": "127.0.0.1", "control_port": 18080,
            "media_host": "127.0.0.1", "return_peer": HELPER["ip"]}

def fake_mesh_stop():
    if HELPER["alive"]:                 # a new helper joins the tailnet with a NEW address
        HELPER["alive"] = False
        HELPER["n"] += 1
        HELPER["ip"] = "100.90.0.%d" % HELPER["n"]
    app.MESH.proc = app.MESH.control_port = app.MESH.tailnet_ip = None

class FakeSession:
    """Records start/stop; everything else the state endpoint reads comes from a real Session."""
    def __init__(self):
        self._real = app.Session()
        self.live = False; self.bridge = None; self.wanted = False; self.starts = 0; self.stops = 0
        self.return_port = 5004; self.return_player = "gstreamer"; self.voice_muted = False
    def __getattr__(self, name):
        return getattr(self._real, name)
    def start(self, host, v, a, return_port=5004, mic_name=None):
        self.live, self.bridge, self.wanted = True, host, True; self.starts += 1
    def stop(self):
        self.live, self.bridge, self.wanted = False, None, False; self.stops += 1
    def voice_sending(self):
        return self.live

STATE = {"token": "PRES", "control_url": CONTROL, "email": "presenter@test"}
passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:300]) if detail else ""))

def run(platform):
    global BASE
    is_win = platform == "windows"
    ses = FakeSession()
    patches = [patch.object(app, "IS_WIN", is_win), patch.object(app, "IS_MAC", not is_win),
               patch.object(app, "api", fake_api), patch.object(app.MESH, "route", fake_route),
               patch.object(app.MESH, "stop", fake_mesh_stop), patch.object(app, "SESSION", ses),
               patch.object(app, "load_state", lambda: dict(STATE)),
               patch.object(app, "save_state", lambda st: None),
               patch.object(app, "av_devices", lambda: {"video": [{"name": "Cam"}], "audio": [{"name": "Mic"}]}),
               patch.object(app, "resolve_by_name", lambda devs, name, default=None: (0, name or "Cam", None)),
               patch.object(app, "_gst", lambda: True), patch.object(app, "_kill_orphan_mesh", lambda **k: None),
               patch.object(app, "_bridge_reachable", lambda *a, **k: True),
               patch.object(app, "_logdir", lambda: LOGS), patch.object(app.time, "sleep", lambda s: None)]
    for p_ in patches:
        p_.start()
    app.PINS.clear()
    srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE = "http://127.0.0.1:%d" % srv.server_port
    def call(path, body=None, method=None):
        req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method=method or ("GET" if body is None else "POST"))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    H = "100.64.0.10"
    print("\n  ==== %s ====" % platform)
    try:
        tool("gate-init"); tool("set", "-", stdin="864200\n"); tool("clear-lockout")

        f0 = len(FLEET_LOG)
        st, b = call("/api/bridges")
        check(st == 200 and b and b[0].get("label") == "NB-001" and b[0].get("ip") == "192.168.1.50",
              "[%s] bridge list comes from /auth/bridges, with its fleet number" % platform, b)
        check(not any("/admin/" in u for _, u, _ in FLEET_LOG[f0:]), "[%s] a presenter never calls /admin/*" % platform)
        AUTH_BRIDGES["exists"] = False
        st, b = call("/api/bridges")
        check(st == 200 and b and b[0]["label"] == "NB-001" and b[0]["ip"] == "192.168.1.50",
              "[%s] an older fleet without /auth/bridges falls back to /admin/devices" % platform, b)
        AUTH_BRIDGES["exists"] = True

        n0 = len(BRIDGE_LOG)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic"})
        check(st == 401 and r.get("need_pin") and ses.starts == 0,
              "[%s] Go live WITHOUT a PIN is refused and nothing starts" % platform, r)
        check(not any(p_ == "/api/set-peer" for _, p_, _ in BRIDGE_LOG[n0:]), "[%s] …the bridge was not even asked" % platform)

        n0 = len(BRIDGE_LOG)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "pin": "111111"})
        unlocks = [x for x in BRIDGE_LOG[n0:] if x[1] == "/api/unlock"]
        check(st == 401 and r.get("reason") == "wrong" and "2 more tries" in r.get("_error", "") and ses.starts == 0,
              "[%s] wrong PIN: refused, '2 more tries', nothing starts" % platform, r)
        check(len(unlocks) == 1, "[%s] …exactly ONE unlock reached the bridge (a verdict is never retried)" % platform, len(unlocks))

        n0 = len(BRIDGE_LOG)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "pin": "864200"})
        check(st == 200 and r.get("ok") and ses.starts == 1, "[%s] right PIN: live" % platform, r)
        sp = [x for x in BRIDGE_LOG[n0:] if x[1] == "/api/set-peer"]
        check(sp and len(sp[-1][2].get("ticket", "")) == 64, "[%s] set-peer carried the session ticket" % platform, sp)
        tk = sp[-1][2].get("ticket", "") if sp else ""
        check(tk and tk not in json.dumps(r), "[%s] the ticket is NOT in the page's reply" % platform)
        ub = [x[2] for x in BRIDGE_LOG[n0:] if x[1] == "/api/unlock"]
        check(ub and all(b.get("protocol") == 2 for b in ub), "[%s] the app asks the bridge for protocol 2" % platform, ub)
        st, s = call("/api/state")
        check(s.get("pin", {}).get("unlocked") is True and tk not in json.dumps(s),
              "[%s] /api/state says unlocked but never shows the ticket" % platform, s.get("pin"))
        gate = json.loads((T / "nft.json").read_text())
        check(gate.get("peer4") == [HELPER["ip"]], "[%s] the bridge's gate admits exactly this app's mesh address" % platform, gate)

        ip_before = HELPER["ip"]
        st, r = call("/api/stop", {"keep_session": True})
        check(st == 200 and r.get("session_kept") and bw.pin_state()["session"]["active"],
              "[%s] a dropped media leg (internal reconnect) keeps the bridge session" % platform, r)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "reconnect": True})
        gate = json.loads((T / "nft.json").read_text())
        check(st == 200 and ses.starts == 2 and gate.get("peer4") == [HELPER["ip"]] and HELPER["ip"] != ip_before,
              "[%s] …reconnects WITHOUT asking again, and the gate follows the new helper address" % platform, (r, gate))

        st, r = call("/api/stop", {})
        check(st == 200 and r.get("bridge_session_ended") and not bw.pin_state()["session"]["active"],
              "[%s] Stop ends the session ON THE BRIDGE (it is locked again)" % platform, r)
        check(json.loads((T / "nft.json").read_text()).get("peer4") == [], "[%s] …and its gate is closed" % platform)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic"})
        check(st == 401 and r.get("need_pin") and ses.starts == 2, "[%s] after Stop, Go live needs the PIN again" % platform, r)

        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "pin": "864200"})
        check(st == 200 and ses.starts == 3, "[%s] live again with the PIN" % platform)
        tool("gate-init")                                        # the bridge rebooted
        st, r = call("/api/stop", {"keep_session": True})
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "reconnect": True})
        check(st == 401 and r.get("need_pin") and r.get("reason") == "no_session" and ses.starts == 3,
              "[%s] after a bridge REBOOT the reconnect asks for the PIN (media not started into a closed gate)" % platform, r)
        st, s = call("/api/state")
        check(s["pin"]["unlocked"] is False and s["pin"].get("lost") == "no_session",
              "[%s] …the dead ticket was dropped" % platform, s.get("pin"))

        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "pin": "864200"})
        check(st == 200 and ses.starts == 4, "[%s] live with the PIN" % platform)
        rc, _ = tool("lock")                                      # an admin locks the bridge
        st, c = call("/api/checks?host=" + H)
        check(c.get("pin", {}).get("locked") is True, "[%s] the bridge reports the admin lock in its checks" % platform, c)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "pin": "864200"})
        check(st == 200 and r.get("resumed") and ses.starts == 4 and bw.pin_state()["session"]["active"],
              "[%s] re-entering the PIN mid-session re-opens the bridge WITHOUT restarting media" % platform, r)
        call("/api/stop", {})

        OLD_BRIDGE["on"] = True
        n0 = len(BRIDGE_LOG)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic", "pin": "864200"})
        check(st == 401 and r.get("reason") == "old_bridge" and ses.starts == 4,
              "[%s] an OLD bridge (cannot check a PIN) is refused, with the reason" % platform, r)
        check(not any(x[1] == "/api/unlock" for x in BRIDGE_LOG[n0:]),
              "[%s] …and its unlock is never called (on old images it restarted all media)" % platform)
        OLD_BRIDGE["on"] = False

        LOCKSTATE_FAILS["n"] = 1
        n0 = len(BRIDGE_LOG)
        st, r = call("/api/unlock", {"host": H, "pin": "864200"})
        check(st == 200 and r.get("ok") and "ticket" not in r,
              "[%s] Studio: /api/unlock survives one transport failure and never returns the ticket" % platform, r)
        st, r = call("/api/golive", {"host": H, "camera_name": "Cam", "mic_name": "Mic"})
        check(st == 200 and ses.starts == 5, "[%s] Studio: go live right after unlocking" % platform, r)

        # the watcher re-points return audio WITH the ticket; a 401 drops the ticket
        app.BRIDGEWATCH.give_up = set()
        with patch.object(app.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                "u", 401, "locked", {}, io.BytesIO(b'{"reason": "superseded"}'))):
            note = app.BRIDGEWATCH._repair("return_audio", {})
        check("Someone else entered the PIN" in (note or "") and not app.PINS.ticket(),
              "[%s] the bridge watcher learns 'superseded' and drops the ticket" % platform, note)
        call("/api/stop", {})

        blob = json.dumps(FLEET_LOG)
        check("864200" not in blob and "111111" not in blob and (not tk or tk not in blob),
              "[%s] neither the PIN nor the ticket was ever sent to the fleet" % platform)
        logs = "".join(p_.read_text(errors="replace") for p_ in LOGS.rglob("*") if p_.is_file())
        check("864200" not in logs and (not tk or tk not in logs), "[%s] …nor written to any app log" % platform)
    finally:
        srv.shutdown(); srv.server_close()
        for p_ in reversed(patches):
            p_.stop()

print("\nPresenter app: the PIN on every go-live (macOS and Windows paths)")
print("=================================================================")
run("macos")
run("windows")
print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
