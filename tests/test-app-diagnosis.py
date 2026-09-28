#!/usr/bin/env python3
"""Does the presenter app blame the RIGHT END when it gives up?

WHY THIS EXISTS
---------------
On 2026-08-14 the app printed, verbatim:

    video_arriving still failing after 3 repairs — this is not something the app can fix;
    check the bridge

while the actual fault was on the Mac it was printed from: the camera daemons had been left
wedged by a process killed mid-capture, so avfoundation handed out the device and delivered
no frames. Voice and return audio were green on the same poll, which means the bridge and the
link were both fine.

Sending an operator to the far end of the link to look for a local fault is the single most
expensive mistake this project keeps making — it is what turned one wedged camera into an
evening of flashing bridge images. So the sentence the supervisor prints when it gives up is
now worth testing.

  python3 tests/test-app-diagnosis.py
"""
import pathlib, re, sys, types

SRC = pathlib.Path(__file__).resolve().parent.parent / "app" / "netbridge-source" / "source_app.py"

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

# Importing the whole app would start threads and bind a port, so lift just the one method
# out of the file and give it the two collaborators it touches. Testing the real source text
# means the test cannot drift away from what ships.
text = SRC.read_text()
m = re.search(r"\n    def _giving_up_because\(self, key, done\):.*?\n(?=    def )", text, re.S)
if not m:
    print("  FAIL  _giving_up_because not found in source_app.py"); sys.exit(1)

ns = {}
guard = types.SimpleNamespace(snapshot=lambda: {"repairs": {}})
# The method also consults SESSION.leg_cpu_rate(), added when the wedged-camera signal landed.
# Without a stand-in the whole file died with NameError - and because it crashed BEFORE
# printing a summary line, the runner recorded it as "no result" rather than a failure and it
# went unnoticed for two phases. A test that cannot fail loudly is not a test.
session = types.SimpleNamespace(leg_cpu_rate=lambda name: None, CPU_FLOOR=0.02)
exec("class W:\n" + m.group(0).rstrip() + "\n", {"GUARD": guard, "SESSION": session}, ns)
W = ns["W"]

def watcher(deaths, others, cpu_rate=None):
    """cpu_rate=None means "no throughput reading available", which is the case the
    death-counting heuristic below must still handle."""
    W._giving_up_because.__globals__["SESSION"] = types.SimpleNamespace(
        leg_cpu_rate=lambda name: cpu_rate, CPU_FLOOR=0.02)
    w = W()
    w.LEG_FOR = {"video_arriving": "video", "voice_arriving": "voice"}
    w.last_checks = others
    g = types.SimpleNamespace(snapshot=lambda: {"repairs": deaths})
    # rebind the module global the method closes over
    W._giving_up_because.__globals__["GUARD"] = g
    return w

print("\nPresenter app — which end does it blame?")
print("=======================================")

print("\n  ---- the 2026-08-14 case ----")
w = watcher({"video": 3}, {"voice_arriving": {"ok": True}, "return_audio": {"ok": True}})
msg = w._giving_up_because("video_arriving", 3)
if "camera" in msg.lower() and "this mac" in msg.lower():
    ok("a dying video leg while voice+return are green -> blames the Mac's camera")
else:
    no("still sends the operator to the bridge for a local camera fault", msg)
if "fix-camera-macos.sh" in msg:
    ok("gives the actual repair command instead of 'check the bridge'")
else:
    no("names the cause but not the fix", msg)
if "check the bridge" not in msg.lower():
    ok("does not say 'check the bridge' when the bridge is demonstrably fine")
else:
    no("still contains the misleading sentence", msg)

print("\n  ---- a leg that dies with nothing else working ----")
w = watcher({"video": 4}, {"voice_arriving": {"ok": False}})
msg = w._giving_up_because("video_arriving", 3)
if "local" in msg.lower() and "camera" not in msg.lower():
    ok("dying leg, nothing green -> local capture/encode, without guessing the camera")
else:
    no("over-claims the camera when there is no evidence the bridge is fine", msg)

print("\n  ---- a leg that stays UP ----")
# A bridge that is not receiving cannot kill our encoder, so zero deaths means the fault is
# downstream. This is the case where 'check the bridge' is the correct advice.
w = watcher({}, {"voice_arriving": {"ok": True}})
msg = w._giving_up_because("video_arriving", 3)
if "downstream" in msg.lower() or "bridge" in msg.lower():
    ok("healthy local leg + nothing arriving -> points downstream (network or bridge)")
else:
    no("fails to point anywhere useful when the fault really is remote", msg)

print("\n  ---- negative control ----")
a = watcher({"video": 3}, {"voice_arriving": {"ok": True}})._giving_up_because("video_arriving", 3)
b = watcher({}, {"voice_arriving": {"ok": True}})._giving_up_because("video_arriving", 3)
if a != b:
    ok("the two situations produce different sentences — the test can tell them apart")
else:
    no("same message either way; this proves nothing", a)

print("\n  ---- a leg that is ALIVE but doing no work ----")
# The heuristic below counts DEATHS. A wedged camera leaves ffmpeg running happily while
# avfoundation delivers nothing, so it dies never and the death count stays zero - which is
# how "check the bridge" got printed for a fault on the Mac it was printed from.
w = watcher({}, {"voice_arriving": {"ok": True}}, cpu_rate=0.004)
msg = w._giving_up_because("video_arriving", 3)
if "LOCAL_CAMERA_FAULT" in msg and "fix-camera-macos.sh" in msg:
    ok("an alive-but-idle video leg is named a LOCAL camera fault")
else:
    no("a wedged camera that never dies is still blamed on the far end", msg[:90])
w = watcher({}, {"voice_arriving": {"ok": True}}, cpu_rate=0.33)
msg = w._giving_up_because("video_arriving", 3)
if "LOCAL_CAMERA_FAULT" not in msg:
    ok("a leg doing real work (0.33 CPU-s/s) is NOT blamed on the camera")
else:
    no("would blame the camera on a healthy encoder — false positives train people to ignore it")

print("\n  ---- the camera is released, not killed ----")
# The wedge that cost the 2026-08-14 session was ffmpeg being SIGKILLed while it held
# avfoundation. Everything below is checked against the real source text, because the failure
# is only visible in the ORDER of the shutdown steps.
if "stdin=subprocess.PIPE" in text:
    ok("media legs are spawned with a stdin pipe (so they can be asked to quit)")
else:
    no("no stdin pipe — the only way to stop ffmpeg is a signal")

# Exercise process behavior: a separate in-process audio-player branch legitimately
# terminates before the subprocess branch, so textual .find() ordering is misleading.
import ast
module = ast.parse(text)
quit_node = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_quit")
quit_ns = {}
exec(compile(ast.Module(body=[quit_node], type_ignores=[]), str(SRC), "exec"), quit_ns)
class Process:
    def __init__(self, succeeds_at):
        self.events = []; self.succeeds_at = succeeds_at; self.stdin = self; self.closed = False
    def poll(self): return None
    def write(self, data): self.events.append("ask")
    def flush(self): pass
    def wait(self, timeout):
        if self.events[-1] == self.succeeds_at: return 0
        raise TimeoutError()
    def terminate(self): self.events.append("term")
    def kill(self): self.events.append("kill")
for succeeds_at, expected in [("ask", ["ask"]), ("term", ["ask", "term"]),
                               (None, ["ask", "term", "kill"])]:
    proc = Process(succeeds_at)
    quit_ns["_quit"](proc)
    if proc.events == expected: ok("graceful shutdown escalation: %s" % expected)
    else: no("incorrect subprocess shutdown sequence", proc.events)

for fn, why in (("def stop", "full teardown"), ("def respawn_leg", "single-leg repair")):
    seg = text[text.find(fn):]
    seg = seg[:seg.find("\n    @_locked", 10) if "\n    @_locked" in seg[10:] else 3000]
    if "_quit(" in seg:
        ok("%-16s uses the polite quit (%s)" % (fn.replace("def ",""), why))
    else:
        no("%s still stops processes without releasing the device" % fn)

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
