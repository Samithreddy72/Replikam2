#!/usr/bin/env python3
"""A dead mesh helper must be visible. It used to make the app look HEALTHIER.

THE FAILURE
-----------
The mesh helper carries the meeting's audio back to the presenter. When it dies, everything
the presenter can see keeps working: the app is up, the camera light is on, the room sees and
hears them. Only the return path is gone — and the presenter cannot tell whether the room is
silent or whether they have been cut off.

`MESH.proc` appeared in exactly ONE place in the entire application: the leg watchdog's first
line, where a dead helper took the same branch as "no session running" and cleared the
missing-legs list. So the app reported `legs.ok = true` precisely because the helper had died.

There was no `mesh` key in /api/state at all. The UI could not distinguish APP OK from MESH
HELPER OK from BRIDGE CONNECTED from RETURN AUDIO OK.
"""
import pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _source import code_only

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_RAW = (ROOT / "app/netbridge-source/source_app.py").read_text()
APP = code_only(APP_RAW)
P = F = 0


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


print("NetBridge mesh helper health")
print("============================\n")

print("  ---- the helper's state must be reported at all ----")
if re.search(r"def health\(self\)", APP):
    ok("MeshManager exposes a health() snapshot")
else:
    no("MeshManager has no health method")

if re.search(r'"mesh":\s*MESH\.health\(\)', APP):
    ok("/api/state carries a `mesh` key, separate from `legs`")
else:
    no("/api/state does not report mesh helper health",
       "one green light covering app+mesh+bridge+return is how a presenter ends up talking "
       "to a room that cannot answer")

print("\n  ---- a dead helper must NOT clear the leg alarm ----")
# Extract the watchdog tick and check the dead-helper branch specifically.
# There are SEVERAL _tick methods (StreamGuard, BridgeWatch, the leg watchdog). The first
# version of this test matched whichever came first and reported a failure in code that was
# already correct. Select the one that actually mentions MESH.proc.
m = None
for cand in re.finditer(r"def _tick\(self\):(.*?)(?=\n    def )", APP, re.S):
    if "MESH.proc" in cand.group(1):
        m = cand
        break
if not m:
    no("could not locate the leg watchdog _tick")
else:
    tick = m.group(1)
    combined = re.search(r"if not SESSION\.live or not MESH\.proc or MESH\.proc\.poll\(\) is not None:", tick)
    if combined:
        no("dead helper still shares the 'no session' branch",
           "clearing `missing` there is what made legs.ok TRUE when the helper died")
    else:
        ok("helper death no longer shares the 'no session' branch")

    # Bound the branch to its own body — up to and including its `return`. Splitting on the
    # `if` and taking everything after it swept in the REST of _tick, where `missing` is
    # legitimately assigned on the normal path, and reported a failure in correct code.
    dm = re.search(r"if not MESH\.proc or MESH\.proc\.poll\(\) is not None:(.*?\n\s*return\b)",
                   tick, re.S)
    if dm:
        dead_branch = dm.group(1)
        if re.search(r"self\.missing\s*,?[^\n]*=\s*\[\]", dead_branch) or \
           re.search(r"self\.missing\s*=\s*\[\]", dead_branch):
            no("the dead-helper branch still clears `missing`",
               "that is the exact line that reported an all-clear on failure")
        else:
            ok("the dead-helper branch does NOT clear `missing`")
        if "self.strikes = 0" in dead_branch:
            ok("it still stops accumulating strikes (nothing useful to probe through a dead helper)")
    else:
        no("could not isolate the dead-helper branch")

print("\n  ---- health() must distinguish the three states ----")
h = re.search(r"def health\(self\):(.*?)(?=\n    def )", APP, re.S)
if not h:
    no("could not read health()")
else:
    body = h.group(1)
    for state, why in (("not_started", "idle is not a fault"),
                       ("missing", "never started is different from died"),
                       ("dead", "exited is the dangerous one"),
                       ("running", "the healthy case")):
        if '"%s"' % state in body:
            ok("reports `%s` (%s)" % (state, why))
        else:
            no("no `%s` state" % state)

    if re.search(r'"ok":\s*False', body):
        ok("failure states carry ok=False, so a caller cannot mistake them for healthy")
    else:
        no("health() never reports ok=False")

    if '"fix"' in body:
        ok("failure states tell the operator what to DO")
    else:
        no("a fault with no remedy is only half a diagnosis")

    if "quarantin" in body.lower():
        ok("names quarantine as a likely cause (the helper is quarantined separately from the app)")
    else:
        no("should name quarantine — it is the most common cause of a helper that will not run")

    if re.search(r"hear nothing back|you will hear nothing", body):
        ok("says plainly what the presenter will experience")
    else:
        no("the detail should describe the SYMPTOM, not just the state name")

    if "SESSION" in body and "wanted" in body:
        ok("idle is keyed off session intent, not off `proc is None`")
    else:
        no("must not report `missing` for the normal idle case")

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
