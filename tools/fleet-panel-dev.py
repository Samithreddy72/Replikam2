#!/usr/bin/env python3
"""Preview the Fleet admin panel from this checkout against a running control plane.

Serves control-plane/panel-dist/index.html straight from disk - read again on every request, so
an edit shows on reload - and forwards every other path to the control plane. The panel calls
relative paths with a bearer token kept in localStorage, so staying same-origin here needs no
CORS, cookie or backend change, and no backend has to run on this machine.

READ-ONLY unless --allow-writes: only sign-in may POST. UI work never needs to change the fleet,
and two admin actions build payload URLs from location.origin - from here they would hand real
bridges an http://127.0.0.1 address.

  python3 tools/fleet-panel-dev.py                    # https://fleet.scine.online on port 5180
  python3 tools/fleet-panel-dev.py --upstream https://fleet.example.com --port 5181

Sign in with the six-digit code from the email. The link in that email opens production.
"""
import argparse
import http.client
import json
import pathlib
import ssl
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL = pathlib.Path(__file__).resolve().parents[1] / "control-plane" / "panel-dist" / "index.html"
SIGN_IN = {"/auth/magic-link", "/auth/magic-redeem", "/auth/redeem"}
# Per-connection headers are never forwarded. Host becomes the upstream's own; without
# Accept-Encoding the upstream answers uncompressed, so bodies pass through untouched.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailer", "transfer-encoding", "upgrade", "host", "accept-encoding"}
# send_response() adds its own.
OWN_RESPONSE_HEADERS = {"server", "date"}


class Preview(BaseHTTPRequestHandler):
    upstream = None          # urllib.parse.SplitResult, set by main()
    allow_writes = False

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.panel()
        if (self.command not in ("GET", "HEAD", "OPTIONS") and path not in SIGN_IN
                and not self.allow_writes):
            # 405, not 403: the panel treats 401/403 as an expired sign-in and signs out.
            return self.reply(405, {"detail": "Local preview is read-only: %s %s was not sent to the "
                                              "fleet. Restart with --allow-writes to change it."
                                              % (self.command, path)})
        self.forward()

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    def panel(self):
        body = PANEL.read_bytes()
        self.send_response(200)
        # The same headers production sends with the page.
        for name, value in (("Content-Type", "text/html; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store, must-revalidate"),
                            ("X-Content-Type-Options", "nosniff"),
                            ("Referrer-Policy", "no-referrer")):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def forward(self):
        up = self.upstream
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = up.netloc
        if up.scheme == "https":
            conn = http.client.HTTPSConnection(up.netloc, timeout=300, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(up.netloc, timeout=300)
        started = False
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            started = True
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP | OWN_RESPONSE_HEADERS:
                    self.send_header(k, v)
            self.end_headers()
            # read1 returns whatever has arrived, so the live event stream flows as it comes.
            while self.command != "HEAD":
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                    # the page navigated away or closed its event stream
        except OSError as exc:
            if not started:
                self.reply(502, {"detail": "Control plane unreachable from the preview: %s" % exc})
        finally:
            conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--upstream", default="https://fleet.scine.online",
                    help="control plane to read from (default: %(default)s)")
    ap.add_argument("--port", type=int, default=5180)
    ap.add_argument("--allow-writes", action="store_true",
                    help="also forward admin actions that change the fleet")
    args = ap.parse_args()
    Preview.upstream = urllib.parse.urlsplit(args.upstream.rstrip("/"))
    Preview.allow_writes = args.allow_writes
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Preview)
    print("Fleet panel preview: http://127.0.0.1:%d/ -> %s (%s)"
          % (args.port, args.upstream, "WRITES ALLOWED" if args.allow_writes else "read-only"),
          file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
