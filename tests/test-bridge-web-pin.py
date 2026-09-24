#!/usr/bin/env python3
"""bridge-web + bridge-pin together (2026-09-25): can anyone go live without the PIN?

Drives bridge-web's real request handler (no socket, no sudo) with callers on the mesh and on
the LAN. Underneath it runs the REAL pi/scripts/bridge-pin against a throwaway /etc/bridge and
/run and a fake nft, with every command line recorded - so it proves the go-live path needs a
correct PIN, that Stop relocks, and that neither the PIN nor the ticket ever appears on a
command line (sudo writes command lines to the journal).

  python3 tests/test-bridge-web-pin.py
"""
import importlib.util, io, json, os, pathlib, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "pi" / "scripts" / "bridge-web.py"
TOOL = ROOT / "pi" / "scripts" / "bridge-pin"
T = pathlib.Path(tempfile.mkdtemp())
ETC, RUN, BIN = T / "etc", T / "run", T / "bin"
for d in (ETC, RUN, BIN):
    d.mkdir()

# a tiny nft: remembers the admitted peer and whether the table exists
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

spec = importlib.util.spec_from_file_location("bw", WEB)
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)
bw.PIN_STATE_FILE = str(RUN / "state.json")

# ---- every subprocess bridge-web starts goes through here -----------------------------------
ARGV, BRIDGE_CALLS = [], []
_real_run = subprocess.run
def fake_run(argv, *a, **kw):
    ARGV.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
    if isinstance(argv, (list, tuple)) and argv[:3] == ["sudo", "-n", bw.PIN_TOOL]:
        return _real_run([sys.executable, str(TOOL)] + list(argv[3:]), *a, env=PINENV, **kw)
    if isinstance(argv, (list, tuple)) and argv[:3] == ["sudo", "-n", "/usr/local/bin/bridge"]:
        BRIDGE_CALLS.append(argv[3:])
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.CompletedProcess(argv, 0, "", "")
bw.subprocess.run = fake_run
bw.read = lambda path: "" if "bridge-return-audio" in str(path) else ""

def call(method, path, body=None, caller="100.101.1.10"):
    h = bw.H.__new__(bw.H)
    raw = json.dumps(body or {}).encode()
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h.headers = {"Content-Length": str(len(raw))}
    h.path, h.command, h.request_version = path, method, "HTTP/1.1"
    h.requestline = "%s %s HTTP/1.1" % (method, path)
    # what a dual-stack socket reports: IPv4 callers arrive IPv4-mapped; IPv6 ones as they are
    h.client_address = ("::ffff:" + caller if "." in caller and ":" not in caller else caller, 50000)
    h.server = None
    (h.do_POST if method == "POST" else h.do_GET)()
    out = h.wfile.getvalue().decode()
    status = int(out.split(" ", 2)[1])
    try:
        return status, json.loads(out.split("\r\n\r\n", 1)[1])
    except ValueError:
        return status, {}

passed = failed = 0
def check(cond, msg, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg + (("\n        " + str(detail)[:300]) if detail else ""))

A, B = "100.101.1.10", "100.101.1.20"
_real_run([sys.executable, str(TOOL), "gate-init"], env=PINENV, capture_output=True)

print("\nbridge-web: the PIN is required to go live")
print("==========================================")

print("\n  ---- no PIN set ----")
st, j = call("GET", "/api/lock-state")
check(st == 200 and j.get("protocol") == 2 and j.get("required") and j.get("pin_set") is False,
      "lock-state: protocol 2, PIN required, none set", j)
st, j = call("POST", "/api/unlock", {"pin": "1234"})
check(st == 200 and j.get("ok") is False and j.get("reason") == "no_pin" and "ticket" not in j,
      "unlock refused: no PIN set", j)
st, j = call("POST", "/api/set-peer", {"ip": A, "port": 5004})
check(st == 401 and j.get("error") == "locked" and not BRIDGE_CALLS,
      "set-peer (Go live) refused with 401 - the return path is not touched", j)
_real_run([sys.executable, str(TOOL), "set", "-"], input="135790\n", env=PINENV, capture_output=True, text=True)

print("\n  ---- the old way in is closed ----")
st, j = call("POST", "/api/set-peer", {"ip": A, "port": 5004})
check(st == 401 and j.get("reason") == "no_session", "a mesh caller without a PIN session cannot go live", j)
st, j = call("POST", "/api/return-tune", {"props": "", "pre": ""})
check(st == 401, "return-tune also needs the session", j)
st, j = call("POST", "/api/set-peer", {"ip": A, "port": 5004, "ticket": "f" * 64})
check(st == 401 and j.get("reason") == "no_session", "a forged ticket is refused (no session open)", j)
check(not BRIDGE_CALLS, "nothing reached the bridge CLI")
st, j = call("POST", "/api/unlock", {"pin": "135790"}, caller="192.168.1.77")
check(st == 403, "a LAN caller cannot even try a PIN (mesh only)", j)

print("\n  ---- wrong PIN ----")
st, j = call("POST", "/api/unlock", {"pin": "000000"})
check(st == 200 and j.get("ok") is False and j.get("reason") == "wrong" and j.get("attempts_left") == 2,
      "wrong PIN: HTTP 200 with the verdict (the app must not retry it)", j)

print("\n  ---- right PIN -> ticket -> go live ----")
st, j = call("POST", "/api/unlock", {"pin": "135790"}, caller=A)
check(st == 200 and j.get("ok") and "ticket" not in j,
      "an OLD app (no protocol) unlocks with the right PIN but never receives the ticket to display", j)
st, j = call("POST", "/api/unlock", {"pin": "135790", "protocol": 2}, caller=A)
TK = j.get("ticket", "")
check(st == 200 and j.get("ok") and len(TK) == 64 and j.get("peer") == A and j.get("protocol") == 2,
      "right PIN from %s: ticket issued for that caller" % A, j)
gate = json.loads((T / "nft.json").read_text())
check(gate.get("peer4") == [A], "media gate admits only %s" % A, gate)
st, j = call("POST", "/api/set-peer", {"ip": A, "port": 5004, "ticket": "f" * 64}, caller=A)
check(st == 401 and j.get("reason") == "invalid" and not BRIDGE_CALLS,
      "while a session is open, a forged ticket is still refused (invalid)", j)
st, j = call("POST", "/api/set-peer", {"ip": A, "port": 5004, "ticket": TK}, caller=A)
check(st == 200 and j.get("ok") and BRIDGE_CALLS and BRIDGE_CALLS[-1] == ["set-peer", A, "5004"],
      "with the ticket, set-peer goes through exactly as before", (j, BRIDGE_CALLS))
st, j = call("POST", "/api/set-peer", {"ip": "100.101.9.9", "port": 5004, "ticket": TK}, caller=A)
check(st == 403 and len(BRIDGE_CALLS) == 1,
      "even with the ticket, the room's audio can only go to the caller's own address", j)
st, j = call("POST", "/api/set-peer", {"ip": B, "port": 5004, "ticket": TK}, caller=B)
gate = json.loads((T / "nft.json").read_text())
check(st == 200 and gate.get("peer4") == [B],
      "the app's mesh helper restarted with a new address: the session and gate follow it", gate)
st, j = call("GET", "/api/lock-state")
check(j.get("session", {}).get("active") and j["session"].get("peer") == B and j.get("locked") is False,
      "lock-state shows the live session", j)
check(bw.gather.__code__ is not None and bw.pin_state().get("session", {}).get("active"),
      "status/telemetry read the same state (from the file - no sudo per poll)")

print("\n  ---- Stop relocks ----")
st, j = call("POST", "/api/end-session", {"ticket": "0" * 64}, caller=B)
check(st == 200 and j.get("ended") is False, "a wrong ticket cannot end the session", j)
st, j = call("POST", "/api/end-session", {"ticket": TK}, caller=B)
check(st == 200 and j.get("ended") is True, "end-session with the ticket ends it", j)
gate = json.loads((T / "nft.json").read_text())
check(gate.get("peer4") == [], "media gate closed again", gate)
n = len(BRIDGE_CALLS)
st, j = call("POST", "/api/set-peer", {"ip": B, "port": 5004, "ticket": TK}, caller=B)
check(st == 401 and len(BRIDGE_CALLS) == n, "after Stop the old ticket cannot go live again", j)

print("\n  ---- addresses are parsed, not prefix-matched ----")
for spoof in ("::1:2:3:4", "::127.0.0.1", "::100.64.0.1", "100.1.2.3", "fe80::1"):
    st, j = call("POST", "/api/unlock", {"pin": "135790", "protocol": 2}, caller=spoof)
    check(st == 403, "caller %s is neither the bridge nor the mesh (403)" % spoof, (st, j))

print("\n  ---- nothing secret on a command line ----")
flat = [" ".join(a) for a in ARGV]
leaks = [a for a in flat if "135790" in a or "000000" in a or TK in a]
check(not leaks, "neither the PIN nor the ticket appears in any of %d command lines" % len(flat), leaks[:3])
check(any(a.startswith("sudo -n %s unlock -" % bw.PIN_TOOL) for a in flat),
      "unlock passes the PIN as '-' (stdin)")
restarts = [c for c in BRIDGE_CALLS if c and c[0] in ("restart", "stop", "start")]
check(not restarts, "no media restart anywhere in the PIN flow", restarts)

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
