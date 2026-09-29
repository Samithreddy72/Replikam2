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

print("\n  ---- every referenced script is actually shipped ----")
# The image installs `for f in pi/scripts/*` into /usr/local/bin, so a script referenced by
# an absolute path only works if a file of that exact name exists in pi/scripts. A typo or a
# rename here produces a command that passes all three gates and then fails on the device
# with "not installed" — visible only to whoever reads the command output.
import re as _re
shipped = {p_.name for p_ in (ROOT / "pi" / "scripts").iterdir() if p_.is_file()}
refs = set()
for f in (AGENT, ROOT / "pi" / "scripts" / "bridge",
          ROOT / "pi" / "scripts" / "bridge-web.py",
          ROOT / "pi" / "scripts" / "bridge-jitter.py"):
    refs |= set(_re.findall(r"/usr/local/bin/([A-Za-z0-9._-]+)", f.read_text()))
missing = sorted(r for r in refs if r not in shipped and r != "bridge")
if not missing:
    ok("all %d referenced /usr/local/bin scripts exist in pi/scripts" % len(refs))
else:
    no("referenced but NOT shipped — the command will fail on the device", missing)
for need in ("bridge-golden.py", "bridge-jitter.py"):
    if need in shipped:
        ok("%s is in pi/scripts, so the image installs it" % need)
    else:
        no("%s would never reach the card" % need)

print("\n  ---- the menu tells the truth about what it costs ----")
# An admin arrives with a symptom and picks the entry that matches it. Two failures make
# that dangerous, and both were present before this check:
#   * an action that interrupts a live meeting without saying so in its label
#   * an action listed as disruptive that never actually asks for confirmation
# The second is subtler: DISRUPTIVE is consulted with the BARE command, so listing
# "jitter-fix:3" alone silently matched nothing and rung 3 would have frozen video on a live
# call with no prompt at all.
_p = PANEL.read_text()
_dis = set(re.findall(r'"([^"]+)"',
           re.search(r"const DISRUPTIVE = new Set\(\[(.*?)\]\)", _p, re.S).group(1)))
_acts = [(v, l) for v, l in re.findall(r'\["([^"]*)",\s*(?:"([^"]*)"|null)\]',
         re.search(r"const ACTIONS = \[(.*?)\n\];", _p, re.S).group(1))
         if v and not v.startswith("#")]
_bad = []
for v, l in _acts:
    prompts = v.split(":")[0] in _dis or v in _dis
    warns = "\u26a0" in l
    if prompts != warns:
        _bad.append("%s (confirms=%s, warns=%s)" % (v, prompts, warns))
if not _bad:
    ok("all %d actions: warning in the label matches confirmation on a live bridge" % len(_acts))
else:
    no("label and behaviour disagree — the cost is discovered after clicking", _bad)

# The distinction the whole jitter design rests on. If these ever collapse into one group
# again, an operator will click the one that tunes the direction they are not listening to.
_headings = [v[1:] for v, _ in re.findall(r'\["(#[^"]*)",\s*(null)\]',
             re.search(r"const ACTIONS = \[(.*?)\n\];", _p, re.S).group(1))]
if any("cannot hear the ROOM" in h for h in _headings) and \
   any("cannot hear YOU" in h for h in _headings):
    ok("the menu separates the two audio DIRECTIONS (they need opposite fixes)")
else:
    no("the two audio directions are not distinguished", _headings)
if _headings and all(h.strip() for h in _headings):
    ok("every action sits under a symptom heading (%d groups)" % len(_headings))
else:
    no("ungrouped actions", _headings)

print("\n  ---- default diagnosis and targeted recovery; advanced tools remain available ----")
_common = set(re.findall(r'"([^"]+)"',
              re.search(r"const COMMON = new Set\(\[(.*?)\]\)", _p, re.S).group(1)))
_all = {v for v, _ in _acts}
for need in ("diagnose", "recover-video", "jitter-diagnose", "set-pin", "clear-lockout", "running", "logs"):
    if need in _common:
        ok("short menu keeps " + need)
    else:
        no("short menu dropped " + need)
for advanced in ("jitter-fix:1", "profile:wan", "reset-clock", "restart", "reboot", "jitter-reset", "golden-save", "golden-restore"):
    if advanced in _all and advanced not in _common:
        ok("advanced action remains available: " + advanced)
    else:
        no("advanced action missing or exposed by default: " + advanced)
if _common <= _all:
    ok("every COMMON entry actually exists in ACTIONS")
else:
    no("COMMON names something not in the menu", sorted(_common - _all))
if len(_common) < len(_all):
    ok("short menu is genuinely shorter (%d of %d shown)" % (len(_common), len(_all)))
else:
    no("the short menu hides nothing — the toggle is pointless")

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
