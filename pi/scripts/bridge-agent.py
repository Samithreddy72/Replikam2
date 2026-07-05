#!/usr/bin/env python3
"""NetBridge fleet agent - stdlib only.

Run once per tick by bridge-agent.timer. Each tick it:
  1. ensures the device is enrolled with the control plane (exchanges a one-time
     bootstrap token for a long-lived per-device token, stored at /etc/bridge/agent.token),
  2. pushes a telemetry heartbeat (the same dict bridge-web.py /api/status serves),
  3. pulls any queued commands, runs an ALLOW-LISTED subset via the `bridge` CLI,
     and reports the result.

Pull model = works behind NAT with no inbound, and every action is auditable.
Config lives in /etc/default/bridge-agent:
    CONTROL_URL=https://control.example.ts.net   # control plane base URL (on the tailnet)
    BOOTSTRAP_TOKEN=...                           # one-time enroll token (injected at flash)
"""
import json, os, ssl, subprocess, urllib.request, urllib.error, importlib.util

CONF = "/etc/default/bridge-agent"
STATE_DIR = "/etc/bridge"
TOKEN_FILE = os.path.join(STATE_DIR, "agent.token")
WEB_PY = "/usr/local/bin/bridge-web.py"
TIMEOUT = 10

# Map control-plane command types -> argv for the existing bridge CLI. Anything not
# in this table is refused, so the control plane can never run arbitrary commands.
ALLOWED = {
    "restart":     lambda a: ["bridge", "restart"],
    "reset-clock": lambda a: ["bridge", "reset-clock"],
    "profile":     lambda a: ["bridge", "profile", _enum(a.get("mode"), ("lan", "wan"))],
    "set-peer":    lambda a: ["bridge", "set-peer", _ip(a.get("ip")), _port(a.get("port", "5004"))],
}


def _enum(v, allowed):
    if v not in allowed:
        raise ValueError("bad value %r (allowed: %s)" % (v, allowed))
    return v


def _ip(v):
    parts = str(v or "").split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ValueError("bad ip %r" % (v,))
    return v


def _port(v):
    if not str(v).isdigit() or not (1 <= int(v) <= 65535):
        raise ValueError("bad port %r" % (v,))
    return str(v)


def load_conf():
    d = {}
    try:
        with open(CONF) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    d[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return d


def telemetry():
    """Reuse bridge-web.py's gather() so there's one source of truth."""
    spec = importlib.util.spec_from_file_location("bridge_web", WEB_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.gather()


def http(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def enroll(base, conf, tel):
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    boot = conf.get("BOOTSTRAP_TOKEN")
    if not boot:
        raise SystemExit("not enrolled and no BOOTSTRAP_TOKEN in %s" % CONF)
    resp = http("POST", base + "/v1/enroll", body={
        "bootstrap_token": boot,
        "device_id": tel["device_id"],
        "pairing_code": tel["pairing_code"],
        "version": tel["version"],
        "tailscale_ip": tel["tailscale_ip"],
        "hostname": tel["host"],
    })
    token = resp["device_token"]
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


def run_command(cmd):
    cid, ctype, args = cmd.get("id"), cmd.get("type"), cmd.get("args") or {}
    builder = ALLOWED.get(ctype)
    if not builder:
        return cid, "rejected", "unknown command type %r" % ctype
    try:
        argv = builder(args)
    except ValueError as e:
        return cid, "rejected", str(e)
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    status = "done" if p.returncode == 0 else "failed"
    return cid, status, (p.stdout + p.stderr)[-2000:]


def main():
    conf = load_conf()
    base = conf.get("CONTROL_URL", "").rstrip("/")
    if not base:
        raise SystemExit("CONTROL_URL not set in %s" % CONF)
    tel = telemetry()
    token = enroll(base, conf, tel)
    # heartbeat
    try:
        http("POST", base + "/v1/telemetry", token=token, body=tel)
    except urllib.error.URLError as e:
        raise SystemExit("telemetry failed: %s" % e)
    # pull + run queued commands
    try:
        cmds = http("GET", base + "/v1/commands", token=token) or []
    except urllib.error.URLError:
        cmds = []
    for c in cmds:
        cid, status, output = run_command(c)
        try:
            http("POST", base + "/v1/commands/%s/result" % cid, token=token,
                 body={"status": status, "output": output})
        except urllib.error.URLError:
            pass


if __name__ == "__main__":
    main()
