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
import json, os, re, ssl, subprocess, time, urllib.request, urllib.error, importlib.util

CONF = "/etc/default/bridge-agent"
STATE_DIR = "/etc/bridge"
TOKEN_FILE = os.path.join(STATE_DIR, "agent.token")
WEB_PY = "/usr/local/bin/bridge-web.py"
TIMEOUT = 10
# Proof of life for the rest of the bridge: touched after every heartbeat the fleet accepted.
# The rollback guard (bridge-overrides.sh) reverts an update of anything the bridge needs to stay
# reachable if this goes stale, and an A/B trial boot is only committed once it exists.
LAST_OK = os.environ.get("BRIDGE_AGENT_LAST_OK", "/run/bridge-agent/last-ok")
# Results of commands that ran outside the agent (see DETACHED). On /data so a result survives
# an agent restart or a reboot and is still reported.
RESULTS = os.environ.get("BRIDGE_AGENT_RESULTS", "/data/agent-results")
CMD_RUN = "/usr/local/bin/bridge-cmd-run.sh"
SYSTEMD_RUN = os.environ.get("BRIDGE_AGENT_SYSTEMD_RUN", "/usr/bin/systemd-run")
# Commands that can outlive one agent tick. The agent is a oneshot that systemd kills after
# TimeoutStartSec, taking its children with it — so a deploy, an update or a media restart
# started inline could be killed half-way and its result was lost ("D2", 2026-08-08). These run
# in their own systemd job (value = max run time, seconds) and are reported on a later tick.
# "update" is 3 h (2026-09-28): one hour had to cover a 1.1 GB download over venue Wi-Fi AND the
# slot write, and systemd killed slow updates half-way through writing the slot. It also covers
# waiting for a live meeting to end before the heavy write. bridge-update.sh BUDGET_S matches it,
# and the fleet's TIMEOUT_S["update"] is this plus time to report.
DETACHED = {"deploy-script": 900, "revert-script": 600, "unquarantine": 600, "update": 10800,
            "diagnose": 900, "restart": 600, "start": 600, "stop": 300, "profile": 600,
            "reset-clock": 300, "gadget-tune": 120, "gadget-tune-clear": 120,
            "jitter-diagnose": 300, "jitter-fix": 600, "jitter-reset": 600,
            "golden-save": 300, "golden-restore": 600}

# Map control-plane command types -> argv for the existing bridge CLI. Anything not
# in this table is refused, so the control plane can never run arbitrary commands.
ALLOWED = {
    "free-space": lambda a: ["journalctl", "--vacuum-size=64M"],
    "restart":     lambda a: ["bridge", "restart"],
    "reset-clock": lambda a: ["bridge", "reset-clock"],
    "profile":     lambda a: ["bridge", "profile", _enum(a.get("mode"), ("lan", "wan"))],
    # No "set-peer" (2026-09-25 audit): from the fleet it pointed the room's microphone at any
    # address with no PIN session. Only the presenter app sets it, through bridge-web, with a ticket.
    # A staged rollout names the image to install; with no source we fall back to
    # the device's own BRIDGE_UPDATE_URL (previous behaviour, unchanged).
    # A whole new OS into the standby slot, then a trial boot that commits itself if healthy
    # and rolls back if not. `version` fetches <fleet>/payloads/ota/<version>; `url`/`source`
    # name any other signed payload; `force` allows it under a live session (normally refused).
    "update":      lambda a: ["sudo", "/usr/local/bin/bridge-update.sh", "--fleet"]
                             + (["--version", _version(a["version"])] if a.get("version") else [])
                             + (["--url", _src(a.get("url") or a.get("source"))]
                                if (a.get("url") or a.get("source")) else [])
                             + (["--force"] if a.get("force") else []),
    "reboot":      lambda a: ["sudo", "systemctl", "reboot"],
    "start":       lambda a: ["bridge", "restart"],
    "stop":        lambda a: ["bridge", "stop"],
    "diagnose":    lambda a: ["sudo", "/usr/local/bin/bridge-diagnose.sh"],
    # Remote script deploy. The payload is NOT trusted here: bridge-deploy-script.sh verifies
    # the detached EC signature before installing, and bridge-run.sh verifies again at every
    # service start. This command only names WHICH script and WHERE to fetch it.
    "deploy-script": lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh",
                                _script_name(a.get("name")), _src(a.get("source"))]
                               + (["--now"] if a.get("now") else []),
    "revert-script": lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh",
                                "--revert", _script_name(a.get("name"))],
    # Audio parameters that can only be applied at boot. The command WRITES the file; a
    # separate reboot applies it. Deliberately two steps — an action that silently reboots a
    # bridge is not something anyone should discover by clicking.
    "gadget-tune":  lambda a: ["sudo", "/usr/local/bin/bridge", "gadget-tune", "set",
                               _tunekey(a.get("key")), _tuneval(a.get("key"), a.get("value"))],
    "gadget-tune-show":  lambda a: ["/usr/local/bin/bridge", "gadget-tune", "show"],
    "gadget-tune-clear": lambda a: ["sudo", "/usr/local/bin/bridge", "gadget-tune", "clear"],
    # Jitter: diagnose names the culprit; fix applies a ladder rung. Rung 1 writes a tuning
    # file the PRESENTER APP picks up on its next poll — the buffer that matters lives on the
    # presenter's laptop, not here, so this is the only path a fleet click has to it.
    "jitter-diagnose": lambda a: ["sudo", "/usr/local/bin/bridge-jitter.py", "diagnose", "--json"],
    "jitter-fix":      lambda a: ["sudo", "/usr/local/bin/bridge-jitter.py", "fix",
                                  "--rung", _rung(a.get("rung", 1)), "--json"],
    "jitter-reset":    lambda a: ["sudo", "/usr/local/bin/bridge-jitter.py", "reset", "--json"],
    # Known-good configuration baseline (Golden Profile). 'save' stamps the current config as
    # the reference; 'restore' puts the RESTORABLE fields back and reports what it could not
    # fix. Neither touches the USB gadget, and restore is a no-op when nothing has drifted,
    # so clicking it on a healthy bridge costs nothing.
    # Provenance travels with the command. An operator who confirmed in the panel is recorded
    # as having done so; a caller that says nothing produces an UNCONFIRMED baseline rather
    # than silently inheriting the authority of a verified one.
    "golden-save":    lambda a: ["sudo", "/usr/local/bin/bridge-golden.py", "save", "--json"]
                                + (["--by", _plain(a.get("by"))] if a.get("by") else [])
                                + (["--confirmed"] if a.get("confirmed") else [])
                                + (["--verified", _plain(a.get("verified"))] if a.get("verified") else [])
                                + (["--force"] if a.get("force") else [])
                                + (["--note", _note(a["note"])] if a.get("note") else []),
    "golden-restore": lambda a: ["sudo", "/usr/local/bin/bridge-golden.py", "restore", "--json"],
    # PIN gate (2026-09-25). The agent runs as root, so no sudo: sudo writes every command line
    # to the journal, and set-pin's PIN goes in on STDIN (see STDIN below), never in argv.
    # A session can only be opened by a presenter typing the PIN into the app - the fleet cannot
    # open one. "unlock" survives as an alias of clear-lockout for older panels.
    "set-pin":       lambda a: (_pin(a.get("pin")), ["/usr/local/bin/bridge-pin", "set", "-"])[1],
    "clear-lockout": lambda a: ["/usr/local/bin/bridge-pin", "clear-lockout"],
    "unlock":        lambda a: ["/usr/local/bin/bridge-pin", "clear-lockout"],
    # Ends the live session and closes the media gate. Media services are left running.
    "lock":          lambda a: ["/usr/local/bin/bridge-pin", "lock"],
    # --- remote recovery, added after a night where a bridge in another room could not be
    # --- repaired from the panel at all.
    # Put a quarantined override back. Auto-rollback silently reverts a device to its
    # baked-in script; without this the only way back is mounting the SD card, which for a
    # shipped bridge means it stays reverted forever.
    "unquarantine": lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh",
                               "--unquarantine"] + ([_script_name(a["name"])] if a.get("name") else []),
    # What code is each service ACTUALLY running — override or baked-in, with hashes.
    # READ ONLY. The device re-validates the path against its own allow-listed roots,
    # refuses anything resolving outside them, refuses credential files, and redacts
    # token-shaped values. Reading is the thing that was missing: every question that
    # mattered on 2026-08-13 was a read, and answering one cost an image rebuild.
    "read-file":   lambda a: ["sudo", "/usr/local/bin/bridge-read.py", _readpath(a.get("path"))]
                             + (["--list"] if a.get("list") else [])
                             + (["--lines", _lines(a["lines"])] if a.get("lines") else [])
                             + (["--tail"] if a.get("tail") else []),
    "running":     lambda a: ["sudo", "/usr/local/bin/bridge-deploy-script.sh", "--running"],
    # A light log tail. The diagnostics bundle is right for forensics and wrong for "what
    # just happened"; at ~470KB it is also a poor fit for a slow venue uplink.
    "logs":        lambda a: ["journalctl", "-n", _lines(a.get("lines", 200)),
                              "--no-pager", "-o", "short-iso"]
                             + (["-u", _unit(a["unit"])] if a.get("unit") else []),
}


# Secrets a command needs, fed on stdin so they never appear in argv, ps, or the journal.
STDIN = {"set-pin": lambda a: _pin(a.get("pin")) + "\n"}


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
    """A bare file name — no paths, no traversal. This value becomes a filename under
    /data/overrides on the device, and the device only accepts names listed in its own
    read-only /etc/netbridge/updatable.conf (scripts, Python, keys, unit drop-ins)."""
    v = str(v or "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}", v) or ".." in v:
        raise ValueError("bad script name")
    return v


def _version(v):
    """An image version as the build names it, e.g. 2.1.0-52a161b."""
    v = str(v or "").strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9a-f]{7,40})?", v):
        raise ValueError("bad version %r" % (v,))
    return v

def _plain(v):
    """Free text that will become a command-line argument. Shape only, and deliberately
    narrow: this reaches an argv list rather than a shell, so injection is already
    impossible, but a 200-character cap and a conservative character class keep a mistyped
    payload from ending up embedded in a baseline that outlives the session."""
    v = str(v or "")[:200]
    return re.sub(r"[^A-Za-z0-9 @._,:+-]", "", v)


def _readpath(v):
    """Shape only — the DEVICE enforces the roots, the credential refusals and the redaction.
    Two gates, because this one is reachable by anyone holding an admin token."""
    v = str(v or "")
    if not v.startswith("/"):
        raise ValueError("path must be absolute")
    if len(v) > 256 or "\x00" in v:
        raise ValueError("bad path")
    if not re.fullmatch(r"[A-Za-z0-9/._:\-]+", v):
        raise ValueError("path has unsupported characters")
    return v


def _tunekey(v):
    """Only the two boot-time audio parameters. Not a shell boundary (argv), but a
    blast-radius one: this value selects which knob a remote party may turn."""
    v = str(v or "")
    if v not in ("UAC2_C_SYNC", "UAC2_REQ_NUMBER"):
        raise ValueError("key must be UAC2_C_SYNC or UAC2_REQ_NUMBER")
    return v


def _tuneval(key, v):
    """Validate per key. The device validates again — this is the first of two gates, not
    the only one."""
    v = str(v or "")
    if key == "UAC2_C_SYNC":
        if v not in ("async", "adaptive"):
            raise ValueError("c_sync must be async or adaptive")
        return v
    n = int(v)
    if not (2 <= n <= 64):
        raise ValueError("req_number must be 2..64")
    return str(n)


def _rung(v):
    """Which step of the jitter ladder. Bounded because the rungs differ in DAMAGE, not just
    in strength: rung 3 restarts the bridge's feeders and freezes video."""
    n = int(v or 1)
    if n not in (1, 2, 3):
        raise ValueError("rung must be 1, 2 or 3")
    return str(n)


def _note(v):
    """A human label stored alongside a golden profile ("verified good 13 Aug, clear at all
    three rates"). Bounded and charset-limited: it is written into a JSON file that is read
    back and rendered in the fleet panel, so keep it plain text and keep it short. Argv, not
    a shell, so this is blast-radius rather than a quoting boundary."""
    v = str(v or "").strip()
    if not v:
        raise ValueError("empty note")
    if len(v) > 120:
        raise ValueError("note too long (max 120)")
    if not re.fullmatch(r"[A-Za-z0-9 ,.:;()/_+-]{1,120}", v):
        raise ValueError("note has unsupported characters")
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


LOCAL_STATUS = "http://127.0.0.1:8080/api/status"


def telemetry():
    """The same dict bridge-web.py serves at /api/status - one source of truth.

    Asked of the RUNNING bridge-web first. This agent is a fresh process every 15 s, so
    building the answer in-process meant a cold gather() on every tick: ~24 program launches
    (tailscale twice, systemctl, vcgencmd, pgrep, ...) that no cache could ever serve, hidden
    from per-service CPU accounting because the oneshot's cgroup vanishes when it exits
    (2026-09-22). A cold gather() also has no previous poll to compare against, so every
    stream-liveness field it produced was False. bridge-web keeps both the cache and that
    history. If it is down or answers with something unusable, build it here as before.

    bridge-web runs as 'pi', this agent as root. Every field was checked for a root-only
    source; the one that could plausibly differ is the tailnet address (`tailscale ip`),
    and enrolment reports it to the fleet. So an answer without one is not trusted: build
    it here as root, which costs exactly what every tick cost before.
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(LOCAL_STATUS, timeout=5) as r:
            d = json.loads(r.read().decode())
        if isinstance(d, dict) and d.get("device_id") and d.get("tailscale_ip"):
            return d
    except Exception:
        pass
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
    recovery = None
    recovery_file = "/data/netbridge-recovery.token"
    try:
        import stat
        info = os.lstat(recovery_file)
        if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0:
            with open(recovery_file) as fh: recovery = fh.read(257).strip()
            if len(recovery)>256: recovery=None
    except OSError:
        pass
    resp = http("POST", base + "/v1/enroll", body={
        "recovery_token": recovery,
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
    if recovery:
        try: os.unlink(recovery_file)
        except OSError: pass
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


def _cid_ok(cid):
    return re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid)) is not None


def command_result(ctype, rc, output):
    """Keep structured diagnosis complete; clipping JSON is not a successful result."""
    status = "done" if rc == 0 else "failed"
    if ctype == "jitter-diagnose" and rc == 0:
        if len(output.encode('utf-8')) > 65536:
            return "failed", "Diagnosis exceeded 64 KB; collect a diagnostics bundle for the full evidence."
        try:
            if not isinstance(json.loads(output), dict): raise ValueError('not an object')
        except (ValueError, TypeError):
            return "failed", "Diagnosis did not return valid JSON; collect a diagnostics bundle."
        return status, output
    return status, output[-2000:]


def run_command(cmd):
    """-> (cid, status, output); status None = started in the background, reported later."""
    cid, ctype, args = cmd.get("id"), cmd.get("type"), cmd.get("args") or {}
    builder = ALLOWED.get(ctype)
    if not builder:
        return cid, "rejected", "unknown command type %r" % ctype
    try:
        argv = builder(args)
    except ValueError as e:
        return cid, "rejected", str(e)
    # Check again on the device: a laptop may have connected after the fleet heartbeat.
    disruptive = {"reboot", "restart", "start", "stop", "profile", "reset-clock",
                  "golden-restore", "jitter-fix", "jitter-reset", "update",
                  "deploy-script", "revert-script", "unquarantine"}
    if ctype in disruptive and not (cmd.get("safety") or {}).get("force_live"):
        try:
            current = telemetry()
            streams = current.get("streams") or {}
            pin = current.get("pin") or {}
            busy = (current.get("udc") not in ("not attached", "attached", "powered", "default", "addressed", "configured", "suspended")
                    or not all(isinstance(streams.get(k), bool) for k in ("video", "voice"))
                    or current.get("udc") in ("configured", "suspended")
                    or streams.get("video") or streams.get("voice")
                    or (pin.get("session") or {}).get("active"))
            if busy:
                return cid, "rejected", "Meeting guard: device is busy or its status is unknown; schedule again when idle."
        except Exception:
            return cid, "rejected", "Meeting guard: could not verify local state; no action executed."
    # A command with a secret on stdin always runs inline: the background runner has no stdin.
    if ctype in DETACHED and ctype not in STDIN and _cid_ok(cid) and os.path.exists(SYSTEMD_RUN) \
            and os.path.exists(CMD_RUN):
        try:
            os.makedirs(RESULTS, exist_ok=True)
            with open(os.path.join(RESULTS, "%s.meta" % cid), "w") as f:
                json.dump({"type": ctype, "t": int(time.time())}, f)
            p = subprocess.run([SYSTEMD_RUN, "--unit=bridge-cmd-%s" % cid, "--collect", "--quiet",
                                "--property=RuntimeMaxSec=%d" % DETACHED[ctype],
                                CMD_RUN, str(cid), "--"] + argv,
                               capture_output=True, text=True, timeout=20)
            if p.returncode == 0:
                return cid, None, None
            _forget(cid)
            return cid, "failed", "could not start in the background: " + (p.stdout + p.stderr)[-500:]
        except (OSError, subprocess.SubprocessError) as e:
            _forget(cid)
            return cid, "failed", "could not start in the background: %s" % e
    p = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                       input=STDIN[ctype](args) if ctype in STDIN else None)
    status, output = command_result(ctype, p.returncode, p.stdout + p.stderr)
    return cid, status, output


def _forget(cid):
    for ext in (".meta", ".out", ".rc"):
        try:
            os.remove(os.path.join(RESULTS, "%s%s" % (cid, ext)))
        except OSError:
            pass


def collect_results(base, token):
    """Report background commands that have finished (bridge-cmd-run.sh left <cid>.rc)."""
    try:
        names = os.listdir(RESULTS)
    except OSError:
        return
    now = time.time()
    for n in sorted(names):
        if not n.endswith(".meta"):
            continue
        cid = n[:-5]
        rc_path = os.path.join(RESULTS, cid + ".rc")
        try:
            with open(os.path.join(RESULTS, n)) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        if os.path.exists(rc_path):
            try:
                rc = int(open(rc_path).read().strip() or "1")
            except (OSError, ValueError):
                rc = 1
            try:
                with open(os.path.join(RESULTS, cid + ".out"), "rb") as result_file:
                    if meta.get("type") == "jitter-diagnose":
                        raw = result_file.read(65537)
                    else:
                        result_file.seek(0, os.SEEK_END)
                        result_file.seek(max(0, result_file.tell() - 8000))
                        raw = result_file.read(8000)
                    out = raw.decode("utf-8", "replace")
            except OSError:
                out = ""
            status, out = command_result(meta.get("type"), rc, out)
        elif now - float(meta.get("t", now)) > max(2 * 3600, DETACHED.get(meta.get("type"), 0) + 600):
            # Never before the job's own time limit is up: a 3-hour OS update still running at
            # 2 h is not lost (2026-09-28).
            status, out = "failed", "interrupted: the background job never finished (reboot or power loss?)"
        else:
            continue                      # still running
        try:
            http("POST", base + "/v1/commands/%s/result" % cid, token=token,
                 body={"status": status, "output": out})
        except urllib.error.HTTPError as e:
            if e.code not in (404, 409, 410):
                continue                  # the fleet is having trouble: report on a later tick
        except urllib.error.URLError:
            continue
        _forget(cid)
        if meta.get("type") == "diagnose" and status == "done":
            upload_latest_bundle(base, token)


def mark_ok():
    try:
        os.makedirs(os.path.dirname(LAST_OK), exist_ok=True)
        with open(LAST_OK, "w") as f:
            f.write("%d\n" % time.time())
    except OSError:
        pass


def syslog(msg):
    try:
        subprocess.run(["logger", "-t", "bridge-agent", msg], timeout=5)
    except Exception:
        pass


# How long one tick waits for `tailscale up`. With the CLI's own margin it stays inside the
# agent unit's TimeoutStartSec=45 (pi/systemd/bridge-agent.service).
TAILSCALE_UP_WAIT_S = 20


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
        # --timeout (2026-09-28): `tailscale up` otherwise blocks until the node is Running. When
        # the tailnet's control server is unreachable (a venue or ISP that blocks it) that is
        # forever, and the old 60 s cap outlived the unit's TimeoutStartSec=45, so systemd killed
        # every tick here. tailscaled keeps joining with the key after the CLI gives up waiting.
        argv = ["tailscale", "up", "--authkey", key, "--reset", "--timeout=%ds" % TAILSCALE_UP_WAIT_S]
        host = payload.get("tailscale_hostname") or payload.get("tailscale_host")
        if host:
            argv += ["--hostname", host]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=TAILSCALE_UP_WAIT_S + 10)
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
        mark_ok()
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
            mark_ok()
        else:
            raise SystemExit("telemetry failed: %s" % e)
    except urllib.error.URLError as e:
        raise SystemExit("telemetry failed: %s" % e)
    # report background commands that finished since the last tick
    collect_results(base, token)
    # pull + run queued commands
    try:
        cmds = http("GET", base + "/v1/commands", token=token) or []
    except urllib.error.URLError:
        cmds = []
    for c in cmds:
        cid, status, output = run_command(c)
        if status is None:
            continue                      # running in the background; reported by collect_results
        try:
            http("POST", base + "/v1/commands/%s/result" % cid, token=token,
                 body={"status": status, "output": output})
        except urllib.error.URLError:
            pass
        # A completed diagnose leaves a bundle on disk; ship it to the control
        # plane so the panel's "Download bundle" works without SSH. Best-effort.
        if c.get("type") == "diagnose" and status == "done":
            upload_latest_bundle(base, token)
    # Apply one-time provisioning (a mesh key) LAST (2026-09-28). It used to run before the
    # command pull, so a `tailscale up` stuck on an unreachable tailnet got the tick killed by
    # systemd before commands were fetched: telemetry said "online" while logs, diagnose and
    # reboot never reached the bridge. A stuck mesh must never starve the command path.
    apply_provision(base, token)


if __name__ == "__main__":
    main()
