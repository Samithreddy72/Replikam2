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
import json, os, re, ssl, subprocess, urllib.request, urllib.error, importlib.util

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
    # A staged rollout names the image to install; with no source we fall back to
    # the device's own BRIDGE_UPDATE_URL (previous behaviour, unchanged).
    "update":      lambda a: ["sudo", "/usr/local/bin/bridge-update.sh"]
                             + ([_src(a["source"])] if a.get("source") else []),
    "reboot":      lambda a: ["sudo", "systemctl", "reboot"],
    "start":       lambda a: ["bridge", "restart"],
    "stop":        lambda a: ["bridge", "stop"],
    "diagnose":    lambda a: ["sudo", "/usr/local/bin/bridge-diagnose.sh"],
    # Remote script deploy. The payload is NOT trusted here: bridge-deploy-script.sh verifies
    # the detached EC signature before installing, and bridge-run.sh verifies again at every
    # service start. This command only names WHICH script and WHERE to fetch it.
    "deploy-script": lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh",
                                _script_name(a.get("name")), _src(a.get("source"))],
    "revert-script": lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh",
                                "--revert", _script_name(a.get("name"))],
    "set-pin":     lambda a: ["sudo", "bridge-pin", "set", _pin(a.get("pin"))],
    "unlock":      lambda a: ["sudo", "bridge-pin", "unlock", _pin(a.get("pin"))],
    "lock":        lambda a: ["sudo", "bridge-pin", "lock"],
    # --- remote recovery, added after a night where a bridge in another room could not be
    # --- repaired from the panel at all.
    # Put a quarantined override back. Auto-rollback silently reverts a device to its
    # baked-in script; without this the only way back is mounting the SD card, which for a
    # shipped bridge means it stays reverted forever.
    "unquarantine": lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh",
                               "--unquarantine"] + ([_script_name(a["name"])] if a.get("name") else []),
    # What code is each service ACTUALLY running — override or baked-in, with hashes.
    "running":     lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh", "--running"],
    # A light log tail. The diagnostics bundle is right for forensics and wrong for "what
    # just happened"; at ~470KB it is also a poor fit for a slow venue uplink.
    "logs":        lambda a: ["journalctl", "-n", _lines(a.get("lines", 200)),
                              "--no-pager", "-o", "short-iso"]
                             + (["-u", _unit(a["unit"])] if a.get("unit") else []),
}


def _lines(v):
    """Bounded log tail — a remote operator on a venue uplink should not be able to ask for
    the whole journal by accident."""
    n = int(v or 200)
    if not (1 <= n <= 2000):
        raise ValueError("lines must be 1-2000")
    return str(n)


def _unit(v):
    """Only our own units. Not a shell boundary (argv, never a shell) but a blast-radius one:
    there is no reason for the fleet to read arbitrary system journals."""
    v = str(v or "")
    if not re.fullmatch(r"bridge-[a-z0-9-]{1,32}", v):
        raise ValueError("bad unit %r" % (v,))
    return v


def _pin(v):
    v = str(v or "")
    if not (v.isdigit() and 4 <= len(v) <= 8):
        raise ValueError("pin must be 4-8 digits")
    return v


def _src(v):
    """Update source handed down by the control plane (staged rollout).

    Passed to subprocess as an ARGV element, never through a shell, so this is
    shape validation rather than the security boundary: bridge-update.sh still
    refuses any source whose manifest does not verify against the on-device OTA
    public key, so a bogus URL cannot install anything."""
    v = str(v or "").strip()
    if len(v) > 512:
        raise ValueError("update source too long")
    if not re.match(r"^(https://|http://|file://|/)[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$", v):
        raise ValueError("bad update source %r" % (v,))
    return v


def _script_name(v):
    """A bare <something>.sh — no paths, no traversal. This value becomes a filename under
    /data/overrides on the device."""
    v = str(v or "")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+\.sh", v) or v.startswith("."):
        raise ValueError("bad script name")
    return v

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


def enroll(base, conf, tel, force=False):
    """Return this device's token for `base`, enrolling if we do not have a usable one.

    `force` discards the stored token first. That matters when the control plane MOVES: the
    token we hold was issued by the OLD fleet, the new one has never seen it, and every call
    401s forever while the bridge sits silently offline with nothing to explain it. Without a
    way to re-enroll, migrating a fleet means physically rewriting every card."""
    if force and os.path.exists(TOKEN_FILE):
        try:
            os.replace(TOKEN_FILE, TOKEN_FILE + ".rejected")
            syslog("device token rejected by %s - discarded, re-enrolling" % base)
        except OSError:
            pass
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


def upload_latest_bundle(base, token):
    """POST the newest diagnostics bundle to the control plane (b64; ~15-50KB)."""
    import base64, glob
    try:
        bundles = sorted(glob.glob("/home/pi/diagnostics/bundle-*.tgz"))
        if not bundles:
            return
        path = bundles[-1]
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > 5 * 1024 * 1024:
            syslog("bundle %s too large to upload (%d bytes)" % (path, len(data)))
            return
        http("POST", base + "/v1/diagnostics", token=token,
             body={"filename": os.path.basename(path),
                   "data_b64": base64.b64encode(data).decode()})
        syslog("uploaded diagnostics %s (%d bytes)" % (os.path.basename(path), len(data)))
    except Exception as e:
        syslog("bundle upload failed: %s" % e)


def run_command(cmd):
    cid, ctype, args = cmd.get("id"), cmd.get("type"), cmd.get("args") or {}
    builder = ALLOWED.get(ctype)
    if not builder:
        return cid, "rejected", "unknown command type %r" % ctype
    try:
        argv = builder(args)
    except ValueError as e:
        return cid, "rejected", str(e)
    p = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    status = "done" if p.returncode == 0 else "failed"
    return cid, status, (p.stdout + p.stderr)[-2000:]


def syslog(msg):
    try:
        subprocess.run(["logger", "-t", "bridge-agent", msg], timeout=5)
    except Exception:
        pass


def apply_provision(base, token):
    """Pull the one-time provisioning payload issued at claim and apply it.
    Safe to call every tick: the server returns {"provision": null} once consumed.
    Recognised keys: tailscale_auth_key (+ optional tailscale_hostname). Unknown
    keys are logged and ignored. Never raises into the tick."""
    try:
        resp = http("GET", base + "/v1/provision", token=token)
    except urllib.error.URLError:
        return
    payload = (resp or {}).get("provision")
    if not payload:
        return
    syslog("provision received: keys=%s" % ",".join(sorted(payload.keys())))
    # Accept BOTH spellings. The docs/schema/claim examples all say
    # `tailscale_auth_key` (canonical), but this code used to read only
    # `tailscale_authkey` — so a claim that followed the docs silently did
    # nothing and the bridge never joined the mesh. Take either, canonical first.
    key = payload.get("tailscale_auth_key") or payload.get("tailscale_authkey")
    if key:
        argv = ["tailscale", "up", "--authkey", key, "--reset"]
        host = payload.get("tailscale_hostname") or payload.get("tailscale_host")
        if host:
            argv += ["--hostname", host]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            syslog("provision: tailscale up rc=%d %s" % (p.returncode, (p.stderr or "")[:120]))
        except Exception as e:
            syslog("provision: tailscale up failed: %s" % e)
    else:
        syslog("provision: no actionable keys (nothing to do)")


def main():
    conf = load_conf()
    base = conf.get("CONTROL_URL", "").rstrip("/")
    if not base:
        # An unprovisioned card is a NORMAL state, not a failure - every freshly flashed
        # card is unprovisioned until it is claimed. Exiting non-zero made systemd log a
        # failed unit on every timer tick (29x on the 2026-07-24 test card) and made a
        # healthy card look broken in `systemctl --failed`.
        print("CONTROL_URL not set in %s - card not provisioned yet; nothing to do" % CONF)
        raise SystemExit(0)
    tel = telemetry()
    token = enroll(base, conf, tel)
    # heartbeat
    try:
        # The setup-AP passphrase is reported here, NOT inside gather(): gather() is what
        # the device serves on its unauthenticated LAN endpoint /api/status, and the label
        # secret must never appear there. Sent on the authenticated fleet channel only, so
        # an admin can reprint the label after a reflash regenerates it (build-ledger E1).
        body = dict(tel)
        try:
            body["setup_pass"] = subprocess.run(
                ["/usr/local/bin/bridge-derive-pass"], capture_output=True, text=True,
                timeout=10).stdout.strip() or None
        except Exception:
            body["setup_pass"] = None
        http("POST", base + "/v1/telemetry", token=token, body=body)
    except urllib.error.HTTPError as e:
        # 401 = this control plane does not recognise our token. That is what a fleet MOVE
        # looks like from the device: the token was issued by the old control plane and the
        # new one has never seen it, so every call fails forever and the bridge sits silently
        # offline. Discard it and enrol once with the bootstrap token; if that also fails,
        # report honestly rather than looping on a credential that cannot work.
        if e.code in (401, 403):
            syslog("telemetry %s from %s - re-enrolling" % (e.code, base))
            token = enroll(base, conf, tel, force=True)
            http("POST", base + "/v1/telemetry", token=token, body=body)
        else:
            raise SystemExit("telemetry failed: %s" % e)
    except urllib.error.URLError as e:
        raise SystemExit("telemetry failed: %s" % e)
    # apply one-time provisioning issued at claim
    apply_provision(base, token)
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
        # A completed diagnose leaves a bundle on disk; ship it to the control
        # plane so the panel's "Download bundle" works without SSH. Best-effort.
        if c.get("type") == "diagnose" and status == "done":
            upload_latest_bundle(base, token)


if __name__ == "__main__":
    main()
