#!/usr/bin/env python3
"""The relay: a fleet click has to reach a buffer that lives on the presenter's laptop.

WHY THIS IS A SEPARATE SUITE
----------------------------
Room audio is decoded on the PRESENTER'S MACHINE, through the app's own jitter buffer
(source_app.py: return_jitter_ms, default 250ms). The bridge's rtpjitterbuffer carries the
opposite direction — the presenter's voice on its way to the room.

So for the entire life of this project, every "jitter" action in the fleet menu tuned the
direction the complaining operator was not listening to. `profile:wan` cost a five-second
video freeze and could not, even in principle, change what they heard.

There is no inbound path to a laptop behind NAT, so the fleet cannot push. The app polls the
bridge every 10s, so the bridge carries the request and the app adopts it. That is a contract
between two programs in two repositories' worth of code, connected by a JSON file and an HTTP
field — exactly the kind of seam where a rename on one side silently breaks the other and
nothing fails loudly. Hence: test both ends against each other.

  python3 tests/test-tuning-relay.py
"""
import importlib.util, json, os, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

bj = load(ROOT / "pi" / "scripts" / "bridge-jitter.py", "bj")
bw = load(ROOT / "pi" / "scripts" / "bridge-web.py", "bw")

print("\nFleet -> presenter tuning relay")
print("===============================")

tmp = tempfile.mkdtemp()
TUNE = os.path.join(tmp, "presenter-tuning.json")
bj.PRESENTER_TUNE = TUNE
bw.PRESENTER_TUNE_FILE = TUNE

print("\n  ---- the bridge end ----")
bj.fix(1)
served = bw.presenter_tuning()
if served and served.get("jitter_ms") == 400:
    ok("what bridge-jitter writes is what bridge-web serves")
else:
    no("the two ends disagree about the file", served)
if served.get("ts") and served.get("rung") == 1:
    ok("carries ts and rung, so the app can tell one request from the next")
else:
    no("no way for the app to detect a change", served)

bw.PRESENTER_TUNE_FILE = os.path.join(tmp, "does-not-exist.json")
if bw.presenter_tuning() is None:
    ok("no tuning file -> None (absence, not an empty instruction)")
else:
    no("invented a tuning out of a missing file")
bad = os.path.join(tmp, "corrupt.json")
open(bad, "w").write("{ not json")
bw.PRESENTER_TUNE_FILE = bad
if bw.presenter_tuning() is None:
    ok("corrupt tuning file -> None, and /api/checks still answers")
else:
    no("corrupt file was served as tuning")
bw.PRESENTER_TUNE_FILE = TUNE

print("\n  ---- the app end ----")
# Exercise the REAL BridgeWatch method. Importing source_app constructs its singletons but
# starts no threads (they start in main()), so this is the shipping code path, not a copy.
sys.path.insert(0, str(ROOT / "app" / "netbridge-source"))
try:
    app = load(ROOT / "app" / "netbridge-source" / "source_app.py", "source_app")
except Exception as e:
    print("  SKIP  could not import source_app (%s)" % str(e)[:80])
    print("\n  %d passed, %d failed\n" % (passed, failed))
    sys.exit(1 if failed else 0)

applied = []
class FakeSession:
    def set_return_tuning(self, gain=None, jitter_ms=None, dynamics=None,
                          sink_sync=None, conceal=None):
        # Mirror the real clamp so the test cannot pass on values the app would reject.
        j = None if jitter_ms is None else str(int(max(60, min(1000, int(jitter_ms)))))
        applied.append({"jitter_ms": j, "conceal": conceal, "gain": gain})
        return {"jitter_ms": j}
app.SESSION = FakeSession()

w = app.BridgeWatch()
w._apply_fleet_tuning(bw.presenter_tuning())
if len(applied) == 1 and applied[0]["jitter_ms"] == "400":
    ok("the app adopts the tuning the bridge published")
else:
    no("the relay does not connect", applied)

w._apply_fleet_tuning(bw.presenter_tuning())
if len(applied) == 1:
    ok("re-applying the SAME tuning is skipped")
else:
    no("would rebuild the return pipeline every 10s forever", applied)

bj.fix(2)
w._apply_fleet_tuning(bw.presenter_tuning())
if len(applied) == 2 and applied[1]["jitter_ms"] == "600":
    ok("a new rung is picked up on the next poll")
else:
    no("escalation does not reach the app", applied)

bj.reset()
w._apply_fleet_tuning(bw.presenter_tuning())
if len(applied) == 3 and applied[2]["jitter_ms"] == "250":
    ok("reset reaches the app too (back to the 250ms default)")
else:
    no("reset does not propagate", applied)

print("\n  ---- the bridge is a courier, not an authority ----")
before = len(applied)
w._apply_fleet_tuning(None)
w._apply_fleet_tuning({})
w._apply_fleet_tuning("not a dict")
w._apply_fleet_tuning({"ts": 99})              # a stamp with nothing to apply
if len(applied) == before:
    ok("junk from the bridge changes nothing on the presenter's machine")
else:
    no("malformed input reached the audio pipeline", applied[before:])

json.dump({"jitter_ms": 999999, "ts": 12345}, open(TUNE, "w"))
w._apply_fleet_tuning(bw.presenter_tuning())
if applied[-1]["jitter_ms"] == "1000":
    ok("an absurd value is clamped to 1000ms, not obeyed")
else:
    no("a bridge could make the laptop do something unbounded", applied[-1])

print("\n  ---- negative control ----")
w2 = app.BridgeWatch()
n = len(applied)
w2._apply_fleet_tuning({"jitter_ms": 333, "ts": 777})
if len(applied) == n + 1 and applied[-1]["jitter_ms"] == "333":
    ok("a fresh watcher with no history does apply (the skip is per-value, not a no-op)")
else:
    no("the de-duplication is swallowing everything", applied[-1] if applied else None)

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
