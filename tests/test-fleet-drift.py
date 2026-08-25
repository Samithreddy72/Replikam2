#!/usr/bin/env python3
"""The drift checker must never say a device is fine when it is not.

This tool exists because the P0 security fix sat in git, fully tested and recorded as
SOFTWARE VERIFIED, while the bridge ran an image built before it. The only thing that would
have caught it was asking the device. So the one behaviour that matters here is the direction
of failure: when in doubt, NEVER report "in sync".

Serves a fake /api/status on a local port and drives the real script against it.
"""
import http.server, json, os, pathlib, subprocess, socket, subprocess as sp, sys, threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "fleet-drift-check.sh"
P = F = 0


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(payload, port):
    """A stand-in bridge. `payload=None` means 'answers the socket but sends nothing useful',
    which is a different failure from 'refuses the connection' and must also be handled."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if payload is None:
                self.send_response(500)
                self.end_headers()
                return
            b = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    class S(http.server.HTTPServer):
        # Without this each shutdown leaves the port in TIME_WAIT and the next serve() in this
        # file dies with EADDRINUSE partway through -- which under the hardened runner shows up
        # as NO RESULT rather than as a pass, but is still a broken test.
        allow_reuse_address = True

    srv = S(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run(host):
    r = sp.run(["bash", str(TOOL), host], capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode, r.stdout + r.stderr


def git(*a):
    return sp.run(["git"] + list(a), capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()


print("NetBridge fleet drift checker")
print("=============================\n")

head = git("rev-parse", "--short", "HEAD")

# The script talks to <host>:8080, so the mock has to own that port on a loopback alias.
# Rather than fight for 8080, drive the script's own default-host override.
port = 8080
try:
    srv = serve({"version": "2.0.0-" + head, "device_id": "TESTDEV"}, port)
except OSError as e:
    print("  SKIPPED - port 8080 is in use (%s); cannot host the mock bridge" % e)
    raise SystemExit(0)

print("  ---- a device running exactly HEAD ----")
rc, out = run("127.0.0.1")
if rc == 0 and "IN SYNC" in out:
    ok("device on HEAD -> exit 0, reported IN SYNC")
else:
    no("device on HEAD should be in sync", "rc=%s\n%s" % (rc, out[-300:]))
srv.shutdown(); srv.server_close()

print("\n  ---- a device behind on the P0 security commit (the real S-1 case) ----")
# 182eaea is the P0 commit; its parent is a tree that lacks the fix.
parent = git("rev-parse", "--short", "182eaea^")
srv = serve({"version": "2.0.0-" + parent, "device_id": "TESTDEV"}, port)
rc, out = run("127.0.0.1")
if rc == 2:
    ok("device missing the P0 fix -> exit 2 (security drift), not 0 and not 1")
else:
    no("a device missing the security fix must exit 2", "rc=%s\n%s" % (rc, out[-400:]))
if "SECURITY-RELEVANT" in out and "182eaea" in out:
    ok("names the specific security commit the device is missing")
else:
    no("must name the missing security commit", out[-300:])
if "do not put this bridge on a network you do not control" in out:
    ok("tells the operator what NOT to do while it is unfixed")
else:
    no("should state the operational consequence")
srv.shutdown(); srv.server_close()

print("\n  ---- comments must not be mistaken for security changes ----")
# da41726 changed a comment containing the word "authority". The first version of the checker
# flagged it, which is the cry-wolf failure this test exists to prevent.
parent = git("rev-parse", "--short", "da41726^")
srv = serve({"version": "2.0.0-" + parent, "device_id": "TESTDEV"}, port)
rc, out = run("127.0.0.1")
if "da41726" in out and ("SECURITY-RELEVANT" not in out or "182eaea" in out):
    ok("a comment mentioning 'authority' is not treated as a security change")
else:
    no("comment-only match must not be flagged as security drift", out[-300:])
srv.shutdown(); srv.server_close()

print("\n  ---- unknown states must never read as 'fine' ----")
rc, out = run("127.0.0.1")          # nothing listening now
if rc == 3 and "UNKNOWN" in out:
    ok("unreachable bridge -> exit 3 UNKNOWN, never 'in sync'")
else:
    no("an unreachable bridge must be UNKNOWN", "rc=%s\n%s" % (rc, out[-200:]))

srv = serve({"version": "2.0.0-deadbee", "device_id": "TESTDEV"}, port)
rc, out = run("127.0.0.1")
if rc == 3:
    ok("a SHA that is not in this repo -> exit 3, not a silent pass")
else:
    no("unknown SHA must be UNKNOWN", "rc=%s\n%s" % (rc, out[-200:]))
srv.shutdown(); srv.server_close()

srv = serve(None, port)
rc, out = run("127.0.0.1")
if rc == 3:
    ok("a bridge that answers but sends no usable status -> exit 3")
else:
    no("garbage response must be UNKNOWN", "rc=%s\n%s" % (rc, out[-200:]))
srv.shutdown(); srv.server_close()

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
