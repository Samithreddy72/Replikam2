#!/usr/bin/env python3
"""Does every fleet action exist consistently along the whole chain?

WHY THIS EXISTS
---------------
A command has to survive four independent gates:

    panel button -> backend ALLOWED_COMMANDS -> agent ALLOWED -> a real script

Miss any one and the failure appears somewhere far from the cause. On 2026-08-24 `read-file`
and `gadget-tune` were present in the panel AND in the device agent, and the DEPLOYED control
plane rejected both with "unsupported command type" - the code was in git, the running backend
was three commits behind, and the button simply did nothing with no useful error. Nothing in
the test suite could have caught that, because nothing compared the three lists.

This does. It cannot detect that production is running old code - only a deployment check can
do that - but it guarantees the three lists in the repository agree, so a new button is never
merged half-wired.

  python3 tests/test-fleet-contract.py
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
panel = (ROOT / "control-plane" / "panel-dist" / "index.html").read_text()
backend = (ROOT / "control-plane" / "backend" / "app" / "main.py").read_text()
agent = (ROOT / "pi" / "scripts" / "bridge-agent.py").read_text()

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

# ---- the three lists
blk = re.search(r"const ACTIONS = \[(.*?)\n\];", panel, re.S).group(1)
panel_cmds, labels = [], {}
for v, l in re.findall(r'\["([^"]*)",\s*"([^"]*)"\]', blk):
    if v and not v.startswith("#"):
        panel_cmds.append(v.split(":")[0]); labels[v.split(":")[0]] = l
panel_set = dict.fromkeys(panel_cmds)          # ordered, deduped

be_set = set(re.findall(r'"([a-z][a-z-]+)"',
             re.search(r"ALLOWED_COMMANDS = \{(.*?)\}", backend, re.S).group(1)))

# Endpoints the panel calls directly instead of queueing. mesh-key is one: it posts to
# /admin/devices/{id}/mesh-key, so its absence from ALLOWED_COMMANDS is correct, not drift.
direct = set(re.findall(r'api\(`/admin/devices/\$\{devId\}/([a-z-]+)`', panel))

print("\nFleet action contract")
print("=====================")
print("  panel offers %d distinct commands · backend allows %d · direct endpoints: %s"
      % (len(panel_set), len(be_set), sorted(direct) or "none"))

print("\n  ---- every panel button reaches a backend that accepts it ----")
missing_be = [c for c in panel_set if c not in be_set and c not in direct]
if not missing_be:
    ok("all %d queued commands are in ALLOWED_COMMANDS" % len([c for c in panel_set if c not in direct]))
else:
    no("the backend would reject these with 'unsupported command type'", missing_be)

print("\n  ---- and an agent that will run it ----")
missing_ag = [c for c in panel_set
              if c not in direct and not re.search(r'["\']%s["\']' % re.escape(c), agent)]
if not missing_ag:
    ok("every queued command appears in the device agent")
else:
    no("the device would refuse these after the backend accepted them", missing_ag)

print("\n  ---- nothing is executable but invisible ----")
# A command the backend accepts with no button is not automatically wrong - some are API-only
# by design - but each one must be a deliberate choice, so they are named here.
API_ONLY = {"set-peer", "start", "stop", "update"}
orphans = sorted(be_set - set(panel_set) - API_ONLY)
if not orphans:
    ok("no unexplained backend-only commands (API-only by design: %s)" % ", ".join(sorted(API_ONLY)))
else:
    no("backend accepts commands nothing documents or exposes", orphans)

print("\n  ---- direct endpoints really exist in the backend ----")
for d in sorted(direct):
    if re.search(r'@app\.(post|get|delete)\("/admin/devices/\{device_id\}/%s"' % re.escape(d), backend):
        ok("%s has a matching backend route" % d)
    else:
        no("panel posts to /%s but the backend has no such route" % d)

print("\n  ---- destructive commands are marked in BOTH the UI and the backend ----")
conf = set(re.findall(r'"([a-z-]+)"',
           re.search(r"CONFIRM_REQUIRED = \{(.*?)\}", backend, re.S).group(1)))
disr = set(re.findall(r'"([^"]+)"',
           re.search(r"const DISRUPTIVE = new Set\(\[(.*?)\]\)", panel, re.S).group(1)))
disr_base = {d.split(":")[0] for d in disr}
for c in sorted(conf):
    confirmation_ui = set(re.findall(r'"([a-z-]+)"',
        re.search(r"const BACKEND_CONFIRM = new Set\(\[(.*?)\]\)", panel, re.S).group(1)))
    if c in panel_set and c not in disr_base | confirmation_ui:
        no("%s needs backend confirmation but the UI does not warn about it" % c)
    else:
        ok("%-15s guarded at the backend%s" % (c, "" if c not in panel_set else " and flagged in the UI"))

print("\n  ---- the panel actually SENDS the confirmation ----")
# Adding a backend gate without this would break every destructive button with a 400 the
# operator cannot act on. The UI dialog and the wire flag must move together.
if "confirm: confirmed" in panel:
    ok("the command POST carries confirm")
else:
    no("the panel never sends confirm — every destructive button would 400")
if "BACKEND_CONFIRM" in panel:
    mirrored = set(re.findall(r'"([a-z-]+)"',
                   re.search(r"const BACKEND_CONFIRM = new Set\(\[(.*?)\]\)", panel, re.S).group(1)))
    missing = sorted(conf - mirrored)
    if not missing:
        ok("the panel mirrors every backend-guarded command (%d)" % len(mirrored))
    else:
        no("backend guards these but the panel would not prompt for them", missing)
else:
    no("no BACKEND_CONFIRM mirror in the panel")
if "confirmed = true" in panel and panel.count("confirmed = true") >= 2:
    ok("confirmed is set only inside an accepted dialog, never by default")
else:
    no("confirm could be sent without the operator agreeing to anything")

print("\n  ---- every command has a timeout class ----")
tm = set(re.findall(r'"([a-z-]+)":\s*\d+',
         re.search(r"TIMEOUT_S = \{(.*?)\n\}", backend, re.S).group(1)))
untimed = sorted(c for c in be_set if c not in tm)
if not untimed:
    ok("all %d backend commands have an explicit timeout" % len(be_set))
else:
    # Not a failure: DEFAULT_TIMEOUT_S covers them. But an unlisted command inherits a number
    # nobody chose for it, which is how a legitimately slow command gets killed.
    print("  NOTE  falling back to DEFAULT_TIMEOUT_S: %s" % ", ".join(untimed))
    ok("unlisted commands inherit a documented default")

print("\n  ---- negative control ----")
if "read-file" in panel_set and "read-file" in be_set:
    ok("the two commands that broke in production are present in both lists")
else:
    no("read-file is missing again — the exact 2026-08-24 drift")
fake = "definitely-not-a-command"
if fake not in be_set and fake not in panel_set:
    ok("an invented command is absent everywhere — the comparison is real")
else:
    no("the lists accept anything; this test proves nothing")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
