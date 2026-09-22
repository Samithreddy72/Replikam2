#!/usr/bin/env python3
"""Where does the fleet agent get its telemetry?

The agent is a fresh process every 15 s. It used to import bridge-web.py and run a cold
gather() on every tick - ~24 program launches no cache could serve, and stream-liveness
fields that were always False because a fresh process has no previous poll to compare with.
It now asks the running bridge-web (which has both), and falls back to the in-process build
only when that answer is unavailable or unusable.

  python3 tests/test-agent-telemetry-source.py
"""
import http.server, importlib.util, json, pathlib, sys, tempfile, threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("agent", ROOT / "pi" / "scripts" / "bridge-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
SHIPPED_URL = agent.LOCAL_STATUS          # before the test points it at its own server

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m):
    global failed; failed += 1; print("  FAIL  %s" % m)

# A stand-in bridge-web.py whose gather() is recognisable.
tmp = tempfile.mkdtemp()
fake_web = pathlib.Path(tmp) / "bridge-web.py"
fake_web.write_text("def gather():\n    return {'device_id': 'local-build', 'source': 'in-process'}\n")
agent.WEB_PY = str(fake_web)

reply = {"body": b""}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = reply["body"]
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
agent.LOCAL_STATUS = "http://127.0.0.1:%d/api/status" % srv.server_port

live = {"device_id": "10000000e61fada0", "tailscale_ip": "100.81.160.124", "services": [["bridge-uvcd", "active"]],
        "streams": {"video": True}}
reply["body"] = json.dumps(live).encode()
t = agent.telemetry()
(ok if t == live else no)("uses the running bridge-web's answer when it is up (%s)" % t.get("device_id"))

reply["body"] = b"<html>not json</html>"
t = agent.telemetry()
(ok if t.get("source") == "in-process" else no)("falls back to in-process gather() on a non-JSON reply")

reply["body"] = json.dumps({"error": "starting"}).encode()
t = agent.telemetry()
(ok if t.get("source") == "in-process" else no)("falls back when the reply has no device_id")

reply["body"] = json.dumps(dict(live, tailscale_ip="")).encode()
t = agent.telemetry()
(ok if t.get("source") == "in-process" else no)(
    "falls back (builds as root) when the non-root answer has no tailnet address")

srv.shutdown(); srv.server_close()
t = agent.telemetry()
(ok if t.get("source") == "in-process" else no)("falls back when bridge-web is not listening")

(ok if SHIPPED_URL.startswith("http://127.0.0.1:8080/") else no)(
    "the shipped URL is the local bridge-web only (%s)" % SHIPPED_URL)

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
