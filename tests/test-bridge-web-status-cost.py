#!/usr/bin/env python3
"""How many programs does one /api/status request launch on the bridge?

WHY THIS EXISTS
---------------
On 2026-09-22 bridge-web.py was measured on a live bridge at ~33% of a core - more than either
audio pipeline - because every /api/status launched ~20 programs (hostname, one systemctl per
service, `tailscale ip -4` TWICE, pgrep twice, vcgencmd, sudo, and a journal pipeline that sh()
refuses, followed by a `logger` launch to report the refusal). It is polled every few seconds
by the presenter app, the fleet agent and diagnostic tools.

This counts launches through sh() and subprocess.run with a fake clock, so it checks behaviour
(what actually runs) rather than grepping the source.

  python3 tests/test-bridge-web-status-cost.py
"""
import importlib.util, pathlib, sys, types, io, builtins

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-web.py"
spec = importlib.util.spec_from_file_location("bw", SRC)
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m):
    global failed; failed += 1; print("  FAIL  %s" % m)

# ---- instrumentation -------------------------------------------------------------------
calls = []
def fake_sh(cmd):
    calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
    s = calls[-1]
    if s.startswith("systemctl is-active") and len(s.split()) > 3:
        return "\n".join("active" for _ in s.split()[2:])
    if s.startswith("systemctl is-active"):
        return "active"
    if s == "tailscale ip -4":
        return "100.64.0.5\nfd7a::1"
    if s == "hostname -I":
        return "192.168.1.9"
    if "bridge-pin state" in s:
        return '{"pin_set": true}'
    if s.startswith("systemctl show -p NRestarts"):
        return "0"
    if s.startswith("journalctl"):
        return "ok\nALSA: Input/output error\n"
    return ""
bw.sh = fake_sh

# Model a settled Pi with both media processes alive. A fresh CI VM has a short
# uptime and no feeder processes, which deliberately triggers different work.
real_open = builtins.open
def fixture_open(path, *args, **kwargs):
    path = str(path)
    if path == "/proc/uptime": return io.StringIO("3600.0 3600.0")
    if path == "/proc/90001/cmdline": return io.BytesIO(b"gst-launch\0udpsrc\0port=5000")
    if path == "/proc/90002/cmdline": return io.BytesIO(b"gst-launch\0udpsrc\0port=5002")
    return real_open(path, *args, **kwargs)
bw.open = fixture_open
base_sh = bw.sh
def process_fixture(cmd):
    result = base_sh(cmd)
    if calls[-1] == "pgrep -f udpsrc port=5000": return "90001"
    if calls[-1] == "pgrep -f udpsrc port=5002": return "90002"
    return result
bw.sh = process_fixture

logger_runs = []
real_run = bw.subprocess.run
def fake_run(argv, *a, **k):
    if argv and argv[0] == "logger":
        logger_runs.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return real_run(argv, *a, **k)
bw.subprocess.run = fake_run

clock = [1000.0]
bw.time.monotonic = lambda: clock[0]

def reset():
    calls.clear(); logger_runs.clear()
    # getattr: the same test must run - and FAIL on behaviour - against the pre-fix file
    getattr(bw, "_CACHE", {}).clear(); getattr(bw, "_PIDS", {}).clear()
    if hasattr(bw, "_status_cache"):
        bw._status_cache[0], bw._status_cache[1] = 0.0, None

# ---- 1. a burst of requests builds the answer once ------------------------------------
reset()
first = bw.gather()
n_first = len(calls)
clock[0] += 0.5; bw.gather()
clock[0] += 0.5; bw.gather()
n_burst = len(calls) - n_first
(ok if n_burst == 0 else no)("3 requests within 2 s launch programs once (extra launches: %d)" % n_burst)

# ---- 2. steady state after the fast cache expires launches far fewer ------------------
clock[0] += 2.5
before = len(calls); bw.gather(); launched = calls[before:]
# Off-Linux (no /proc) two launches cannot be avoided by the code under test and do not happen
# on the bridge: cpu_serial() falls back to `hostname`, and _pid_for() cannot re-validate a
# remembered pid, so it re-runs pgrep. Report them, but do not count them.
host_only = [] if pathlib.Path("/proc/self/cmdline").exists() else \
    [c for c in launched if c == "hostname" or c.startswith("pgrep")]
steady = len(launched) - len(host_only)
(ok if steady <= 3 else no)("steady-state request launches <= 3 programs on the bridge (got %d; "
                            "first request %d)" % (steady, n_first))
print("        launches: %s%s" % ([c for c in launched if c not in host_only],
      ("  (+ off-Linux only: %s)" % host_only) if host_only else ""))
(ok if sum(1 for c in launched if c == "vcgencmd get_throttled") <= 1 else no)(
    "vcgencmd get_throttled launched at most once per status build")

# ---- 3. specific fixes ---------------------------------------------------------------
(ok if sum(1 for c in calls[:n_first] if c == "tailscale ip -4") == 1 else no)(
    "tailscale ip -4 runs once per rebuild, not twice")
active_calls = [c for c in calls[:n_first] if c.startswith("systemctl is-active") and "watchdog" not in c]
(ok if len(active_calls) == 1 and len(active_calls[0].split()) == 2 + len(bw.SERVICES) else no)(
    "all services checked in ONE systemctl call (%d calls)" % len(active_calls))
(ok if not any("|" in c for c in calls) else no)("no shell pipeline is ever handed to sh()")
(ok if not logger_runs else no)("no `logger` launch to report a refused command (%d)" % len(logger_runs))
(ok if first.get("clock", {}).get("verdict") == "crackle" else no)(
    "journal heuristic actually works now (fixture has 1 I/O error -> crackle): %s" % first.get("clock"))
(ok if first.get("tailscale_ip") == "100.64.0.5" else no)("tailscale_ip parsed: %s" % first.get("tailscale_ip"))
(ok if [s for s, _ in first["services"]] == list(bw.SERVICES) else no)("service list order preserved")

# ---- 4. callers cannot corrupt the cache ---------------------------------------------
reset(); a = bw.gather(); a["host"] = "tampered"; a["services"].append(("x", "y"))
clock[0] += 0.1; b = bw.gather()
(ok if b["host"] != "tampered" and ("x", "y") not in b["services"] else no)(
    "each caller gets its own copy of the cached answer")

# ---- 5. cached slow values do refresh -------------------------------------------------
reset(); bw.gather(); n0 = sum(1 for c in calls if c == "tailscale ip -4")
clock[0] += 31; bw.gather(); n1 = sum(1 for c in calls if c == "tailscale ip -4")
(ok if n1 == n0 + 1 else no)("tailscale IP is re-read after its 30 s cache expires")

# ---- 6. a failing vcgencmd is not relaunched on every request -------------------------
reset()                                   # fake_sh returns "" for vcgencmd = unusable output
for step in (0, 3, 3, 3, 3):              # five status builds over 12 s
    clock[0] += step
    if hasattr(bw, "_status_cache"): bw._status_cache[1] = None
    bw.gather()
nv = sum(1 for c in calls if c == "vcgencmd get_throttled")
(ok if nv == 1 else no)("unusable vcgencmd launched once in 12 s of polling, not every request (%d)" % nv)
clock[0] += 301
if hasattr(bw, "_status_cache"): bw._status_cache[1] = None
bw.gather()
nv2 = sum(1 for c in calls if c == "vcgencmd get_throttled")
(ok if nv2 == 2 else no)("...and retried after 5 minutes (%d)" % nv2)

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
