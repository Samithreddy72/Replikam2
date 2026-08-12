#!/usr/bin/env python3
"""Every fleet button must survive all three gates.

A command travels: panel <select> -> backend ALLOWED_COMMANDS -> device agent ALLOWED.
Each gate refuses anything it does not recognise, which is the right design and also means
a name that is missing from ONE of them produces a button that looks fine, queues fine, and
does nothing. The operator sees "queued restart on Scine Test — command #31" and believes it.

That is not hypothetical: adding golden-save/golden-restore touched all three files, and
forgetting the backend would have shipped two dead menu entries.

  python3 tests/test-command-gates.py
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANEL = ROOT / "control-plane" / "panel-dist" / "index.html"
BACKEND = ROOT / "control-plane" / "backend" / "app" / "main.py"
AGENT = ROOT / "pi" / "scripts" / "bridge-agent.py"

# Handled entirely by the control plane; it never reaches a device, so it is correct for it
# to be absent from the backend command allowlist and from the agent.
PANEL_ONLY = {"mesh-key"}

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))


def panel_actions():
    s = PANEL.read_text()
    m = re.search(r"const ACTIONS = \[(.*?)\n\];", s, re.S)
    if not m:
        return None
    out = set()
    for val in re.findall(r'\[\s*"([^"]*)"\s*,', m.group(1)):
        if not val or val.startswith("#"):
            continue
        out.add(val.split(":")[0])       # "profile:wan" is the `profile` command
    return out


def backend_allowed():
    s = BACKEND.read_text()
    m = re.search(r"ALLOWED_COMMANDS\s*=\s*\{(.*?)\}", s, re.S)
    return set(re.findall(r'"([a-z][a-z0-9-]*)"', m.group(1))) if m else None


def agent_allowed():
    # Deliberately NOT a brace-matched extraction. An earlier version of this check grabbed
    # from "ALLOWED = {" to the first "}", which lands inside the first lambda's dict access
    # and silently truncated the set — it was about to report eleven false CRITICAL
    # mismatches. Anchor on the shape of an entry instead.
    s = AGENT.read_text()
    return set(re.findall(r'^\s{4}"([a-z][a-z0-9-]*)":\s*lambda', s, re.M))


print("\nFleet command gates")
print("===================")

panel, backend, agent = panel_actions(), backend_allowed(), agent_allowed()

for name, val in (("panel ACTIONS", panel), ("backend ALLOWED_COMMANDS", backend),
                  ("agent ALLOWED", agent)):
    if val:
        ok("parsed %s (%d entries)" % (name, len(val)))
    else:
        no("could not parse %s — this check is BLIND" % name)

if not (panel and backend and agent):
    print("\n  %d passed, %d failed\n" % (passed, failed))
    sys.exit(1)

print("\n  ---- every button reaches a device ----")
dead = sorted((panel - PANEL_ONLY) - backend)
if not dead:
    ok("every panel action is in the backend allowlist")
else:
    no("panel buttons the BACKEND will reject (they queue, then nothing happens)", dead)

dead = sorted((panel - PANEL_ONLY) - agent)
if not dead:
    ok("every panel action is in the agent allowlist")
else:
    no("panel buttons the DEVICE will reject (they queue, then nothing happens)", dead)

print("\n  ---- the backend does not accept what no device runs ----")
orphan = sorted(backend - agent)
if not orphan:
    ok("every backend command has a device implementation")
else:
    no("backend accepts commands no device can run", orphan)

print("\n  ---- the additions from Plan A specifically ----")
for cmd in ("golden-save", "golden-restore"):
    where = [n for n, s_ in (("panel", panel), ("backend", backend), ("agent", agent))
             if cmd in s_]
    if len(where) == 3:
        ok("%s passes all three gates" % cmd)
    else:
        no("%s only present in: %s" % (cmd, ", ".join(where) or "nowhere"))

print("\n  ---- negative control: the check must be able to fail ----")
if "definitely-not-a-real-command" not in backend:
    ok("a made-up command is correctly absent from the backend")
else:
    no("the parser is matching things that are not there")
# Prove the comparison itself works, not just that today's data happens to line up.
if sorted({"ghost"} - backend) == ["ghost"]:
    ok("the set comparison detects a missing command")
else:
    no("the comparison logic is broken — this suite proves nothing")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
