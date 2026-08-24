#!/usr/bin/env python3
"""When the operator ends a session, does it actually END?

WHY THIS EXISTS
---------------
On 2026-08-24 an operator ended a session and the camera kept recording. /api/stop returned
{"ok": true}, the encoders died, and seconds later they were back — because `live` was
derived purely from process state:

    @property
    def live(self):
        return any(p.poll() is None for p in self.procs)

A stopped session and a crashed one are indistinguishable under that definition, so every
supervisor in the file did what it exists to do and restarted the legs. The operator had no
way to stop streaming short of quitting the app, and the green camera light stayed on.

Intent cannot be derived from process state. These tests hold the line that it is recorded.

  python3 tests/test-session-stop.py
"""
import ast, pathlib, sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "app" / "netbridge-source" / "source_app.py"
text = SRC.read_text()
tree = ast.parse(text)

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

def body(name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(text, n) or ""
    return ""

print("\nEnding a session actually ends it")
print("=================================")

print("\n  ---- intent is recorded, not inferred ----")
if "wanted = False" in text:
    ok("Session carries an explicit `wanted` flag")
else:
    no("no intent flag — a stop is indistinguishable from a crash")

st = body("stop")
if "self.wanted = False" in st:
    ok("stop() records that the session is no longer wanted")
else:
    no("stop() kills processes without recording intent")

# Ordering matters: a supervisor tick landing between the kill and the flag would see dead
# legs, conclude they crashed, and restart them. This is the whole bug in miniature.
i_flag = st.find("self.wanted = False")
i_kill = max(st.find("_quit("), st.find("stdin.write"))
if i_flag != -1 and (i_kill == -1 or i_flag < i_kill):
    ok("intent is recorded BEFORE anything is killed (no window to race in)")
else:
    no("processes are killed before the flag is set — a tick in between resurrects them")

print("\n  ---- nothing resurrects an ended session ----")
rs = body("respawn_leg")
if 'getattr(self, "wanted", False)' in rs and rs.find("wanted") < rs.find("leg_argv"):
    ok("respawn_leg refuses before it even looks at what to respawn")
else:
    no("respawn_leg will restart a leg the operator stopped")

sr = body("set_return")
if 'getattr(self, "wanted", False)' in sr:
    ok("set_return will not start a player on an ended session")
else:
    no("the return player can be restarted after a stop — and it counts toward `live`")
# The supervisor treats this function's result as "did the repair work". A dict is truthy.
# The window must clear the explanatory comment; 220 chars cut off mid-word and
# failed correct code — the same class of mistake as a too-short grep range.
if "return False" in sr.split('getattr(self, "wanted", False)')[-1][:600]:
    ok("the refusal is falsy, so the supervisor records a failed repair honestly")
else:
    no("refusal returns something truthy — the supervisor logs a success that never happened")

print("\n  ---- a session that is wanted still self-heals ----")
gl = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
starter = next((ast.get_source_segment(text, n) for n in gl
                if 'self.leg_argv = {"video"' in (ast.get_source_segment(text, n) or "")), "")
if "self.wanted = True" in starter:
    ok("going live sets wanted, so real crashes are still repaired")
else:
    no("wanted is never set true — repairs would never happen at all")
si = starter.find("self.wanted = True"); sl = starter.find("self.leg_argv")
if si != -1 and sl != -1 and si < sl:
    ok("wanted is set before the legs exist (no gap where a repair is refused)")
else:
    no("legs can exist before the session is marked wanted")

print("\n  ---- the supervisors stand down too ----")
# Gating only the repair call still let one attempt through during the window where a leg is
# dying but `live` is briefly true, which printed "restart FAILED" - alarming wording for a
# correct refusal. Both supervisors check intent before they decide anything is wrong.
for cls in ("StreamGuard", "BridgeWatch"):
    node = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls), None)
    tick = next((ast.get_source_segment(text, m) for m in (node.body if node else [])
                 if isinstance(m, ast.FunctionDef) and m.name == "_tick"), "")
    if 'getattr(SESSION, "wanted", False)' in tick.split("\n\n")[0]:
        ok("%s stands down on an ended session" % cls)
    else:
        no("%s still supervises a session the operator ended" % cls)

print("\n  ---- negative control ----")
# If the flag were removed, the checks above must fail rather than silently pass.
if 'getattr(self, "wanted", False)' in rs and "wanted" in st:
    ok("the tests read the real source, so deleting the flag breaks them")
else:
    no("tests would pass without the fix present")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
