#!/usr/bin/env python3
"""Fleet agent (2026-09-24): slow commands run outside the agent and still report ("D2"), the
agent records fleet contact, and the new update / deploy options are shaped correctly.
Runs the REAL bridge-agent.py (imported) and the REAL bridge-cmd-run.sh / bridge-update.sh."""
import importlib.util, json, os, pathlib, subprocess, sys, tempfile, time, urllib.error

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
T = pathlib.Path(tempfile.mkdtemp())
os.environ["BRIDGE_AGENT_RESULTS"] = str(T / "results")
os.environ["BRIDGE_AGENT_LAST_OK"] = str(T / "run" / "last-ok")
os.environ["BRIDGE_AGENT_SYSTEMD_RUN"] = str(T / "systemd-run")
spec = importlib.util.spec_from_file_location("agent", REPO / "pi/scripts/bridge-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

passed = failed = 0
def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1; print("  PASS  " + msg)
    else:
        failed += 1; print("  FAIL  " + msg)

# ---- argument shapes -------------------------------------------------------------------------
A = agent.ALLOWED
check(A["update"]({"version": "2.1.0-abc1234"}) ==
      ["sudo", "/usr/local/bin/bridge-update.sh", "--fleet", "--version", "2.1.0-abc1234"],
      "update {version} -> bridge-update.sh --fleet --version V")
check(A["update"]({"url": "https://fleet.example/payloads/ota/2.1.0-abc1234", "force": True})[-3:] ==
      ["--url", "https://fleet.example/payloads/ota/2.1.0-abc1234", "--force"], "update {url, force}")
for bad in ("2.1", "2.1.0-XYZ", "2.1.0;reboot", "../2.1.0"):
    try:
        A["update"]({"version": bad}); check(False, "bad version %r accepted" % bad)
    except ValueError:
        check(True, "bad version %r refused" % bad)
check(A["deploy-script"]({"name": "bridge-web.py", "source": "https://f/payloads", "now": True})[-1] == "--now",
      "deploy-script {now} -> --now")
for good in ("bridge-web.py", "owner_ssh_authorized_keys", "dropin.bridge-web", "bridge", "uvc-raw-setup.sh"):
    try:
        A["deploy-script"]({"name": good, "source": "https://f/p"}); check(True, "name %r accepted" % good)
    except ValueError:
        check(False, "name %r refused" % good)
for bad in ("../etc/passwd", ".hidden", "a/b", "a..b", "", "x" * 81):
    try:
        A["deploy-script"]({"name": bad, "source": "https://f/p"}); check(False, "name %r accepted" % bad)
    except ValueError:
        check(True, "name %r refused" % (bad[:12],))

# ---- a detached command -----------------------------------------------------------------------
calls = []
(T / "systemd-run").write_text("#!/bin/sh\nexit 0\n"); os.chmod(T / "systemd-run", 0o755)
agent.CMD_RUN = str(T / "cmd-run-present")
pathlib.Path(agent.CMD_RUN).write_text("")
real_run = subprocess.run
def fake_run(argv, **kw):
    calls.append(list(argv))
    class P: returncode = 0; stdout = ""; stderr = ""
    return P()
agent.subprocess.run = fake_run
cid, status, out = agent.run_command({"id": "41", "type": "deploy-script",
                                      "args": {"name": "bridge-web.py", "source": "https://f/payloads"}})
argv = calls[-1]
check(status is None, "deploy-script is started in the background (no result posted yet)")
check(argv[0].endswith("systemd-run") and "--unit=bridge-cmd-41" in argv and "--collect" in argv,
      "runs as its own systemd job (bridge-cmd-41, collected)")
check("--property=RuntimeMaxSec=900" in argv, "with a hard time limit")
check(argv[argv.index(agent.CMD_RUN) + 1:argv.index(agent.CMD_RUN) + 3] == ["41", "--"] and
      argv[-2:] == ["bridge-web.py", "https://f/payloads"], "runner gets the id and the exact command")
meta = json.loads((T / "results" / "41.meta").read_text())
check(meta["type"] == "deploy-script", "the job's type is remembered for reporting")
calls.clear()
cid, status, out = agent.run_command({"id": "42", "type": "running", "args": {}})
check(status == "done" and calls and calls[-1][:2] == ["sudo", "/usr/local/bin/bridge-deploy-script.sh"],
      "a quick read ('running') still runs inline and reports at once")
agent.subprocess.run = real_run

# ---- the real runner leaves a result; the agent reports it -----------------------------------
runner = REPO / "pi/scripts/bridge-cmd-run.sh"
env = dict(os.environ, BRIDGE_AGENT_RESULTS=str(T / "results"))
real_run(["bash", str(runner), "41", "--", "sh", "-c", "echo installed bridge-web.py; exit 0"], env=env)
real_run(["bash", str(runner), "43", "--", "sh", "-c", "echo boom >&2; exit 7"], env=env)
(T / "results" / "43.meta").write_text(json.dumps({"type": "update", "t": int(time.time())}))
check((T / "results" / "41.rc").read_text().strip() == "0", "runner writes the exit status last (0)")
check((T / "results" / "43.rc").read_text().strip() == "7", "runner keeps a failing status (7)")
bad = real_run(["bash", str(runner), "4;rm", "--", "true"], env=env, capture_output=True)
check(bad.returncode == 64, "runner refuses an unsafe id")

posts = []
def fake_http(method, url, token=None, body=None):
    posts.append((url, body)); return {}
agent.http = fake_http
agent.upload_latest_bundle = lambda base, token: posts.append(("bundle", None))
(T / "results" / "44.meta").write_text(json.dumps({"type": "restart", "t": int(time.time())}))   # still running
(T / "results" / "45.meta").write_text(json.dumps({"type": "profile", "t": int(time.time()) - 3 * 3600}))  # lost
agent.collect_results("https://fleet", "tok")
by = {u.rsplit("/", 2)[-2]: b for u, b in posts if u != "bundle"}
check(by.get("41", {}).get("status") == "done" and "installed bridge-web.py" in by["41"]["output"],
      "finished job reported as done, with its output")
check(by.get("43", {}).get("status") == "failed" and "boom" in by["43"]["output"], "failed job reported as failed")
check("44" not in by, "a job still running is not reported yet")
check(by.get("45", {}).get("status") == "failed" and "interrupted" in by["45"]["output"],
      "a job that never finished (reboot) is reported as interrupted after 2 h")
left = sorted(p.name for p in (T / "results").iterdir())
check(left == ["44.meta"], "reported jobs are cleaned up, the running one is kept (%s)" % left)

(T / "results" / "46.meta").write_text(json.dumps({"type": "deploy-script", "t": int(time.time())}))
(T / "results" / "46.rc").write_text("0\n"); (T / "results" / "46.out").write_text("ok\n")
def offline(*a, **k): raise urllib.error.URLError("offline")
agent.http = offline
agent.collect_results("https://fleet", "tok")
check((T / "results" / "46.rc").exists(), "fleet unreachable: the result is kept and reported on a later tick")

# ---- proof of life ---------------------------------------------------------------------------
agent.mark_ok()
check((T / "run" / "last-ok").exists(), "agent records an accepted heartbeat (for rollback + A/B)")

# ---- the updater's source handling (real script, dry run) ------------------------------------
conf = T / "bridge-agent"; conf.write_text('CONTROL_URL="https://fleet.scine.online"\n')
def ota(*args):
    r = real_run(["bash", str(REPO / "pi/scripts/bridge-update.sh")] + list(args),
                 env=dict(os.environ, BRIDGE_OTA_DRYRUN="1", BRIDGE_OTA_AGENT_CONF=str(conf)),
                 capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()
rc, out = ota("--fleet", "--version", "2.1.0-abc1234")
check(rc == 0 and out == "source=https://fleet.scine.online/payloads/ota/2.1.0-abc1234 force=0 fleet=1 noreboot=0",
      "update --version fetches from the fleet's payloads/ota/<version>")
rc, out = ota("--version", "2.1.0;x")
check(rc == 64, "updater refuses a malformed version itself too")
rc, out = ota("--url", "https://example/ota/v", "--force")
check(rc == 0 and "source=https://example/ota/v force=1" in out, "update --url + --force")

# ---- a stuck mesh never starves the command path (2026-09-28) ------------------------------------
# A whole tick of the REAL main(), with the network and every subprocess stubbed. The provision
# step used to run first, and `tailscale up` blocks until the node is Running: with the tailnet's
# control server unreachable, systemd killed the tick (TimeoutStartSec) before commands were pulled.
import re as _re
order, ts_calls = [], []
def tick_http(method, url, token=None, body=None):
    path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
    order.append(method + " " + path)
    if path == "/v1/commands":
        return []
    if path == "/v1/provision":
        return {"provision": {"tailscale_auth_key": "tskey-auth-TESTONLY", "tailscale_hostname": "netbridge-T001"}}
    return {}
def tick_run(argv, **kw):
    if argv and argv[0] == "tailscale":
        ts_calls.append((list(argv), kw.get("timeout")))
    class P: returncode = 0; stdout = ""; stderr = ""
    return P()
agent.http = tick_http
agent.subprocess.run = tick_run
agent.load_conf = lambda: {"CONTROL_URL": "https://fleet.test"}
agent.telemetry = lambda: {"version": "2.2.0-test"}
agent.enroll = lambda base, conf, tel, force=False: "tok"
agent.collect_results = lambda base, token: order.append("collect_results")
try:
    agent.main()
finally:
    agent.subprocess.run = real_run
check("GET /v1/commands" in order and "GET /v1/provision" in order
      and order.index("GET /v1/commands") < order.index("GET /v1/provision"),
      "a tick pulls its commands BEFORE applying a mesh key (%s)" % " -> ".join(order))
unit = (REPO / "pi/systemd/bridge-agent.service").read_text()
limit = int(_re.search(r"^TimeoutStartSec=(\d+)", unit, _re.M).group(1))
argv, cap = ts_calls[0] if ts_calls else ([], None)
wait = next((int(a.split("=", 1)[1].rstrip("s")) for a in argv if a.startswith("--timeout=")), None)
check(wait is not None and cap is not None and wait < cap < limit,
      "tailscale up stops waiting (--timeout=%ss, killed at %ss) inside the unit's %ss limit" % (wait, cap, limit))

print("\n  %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
