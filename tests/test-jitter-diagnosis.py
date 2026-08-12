#!/usr/bin/env python3
"""Does the jitter diagnosis name the RIGHT culprit, and refuse to offer a harmful fix?

This matters more than it looks. The fixes in this system are not interchangeable:

  reset-clock   rebuilds the USB gadget — the meeting laptop's camera, mic and speakers all
                vanish and must be re-selected. Correct for a degraded UAC2 clock. A disaster
                applied to a Wi-Fi problem.
  profile:wan   restarts the bridge's feeders — ~5s of frozen video — and tunes the direction
                the operator is NOT listening to.
  jitter-fix 1  raises the presenter's own buffer. No video interruption.

So a wrong diagnosis is not a wasted click, it is an interruption to a live meeting to treat
something that was never the cause. These tests feed known evidence and assert on the
culprit, the recommended action, and — for causes with no software fix — that no action is
offered at all.

  python3 tests/test-jitter-diagnosis.py
"""
import importlib.util, json, os, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "pi" / "scripts" / "bridge-jitter.py"
spec = importlib.util.spec_from_file_location("bj", SRC)
bj = importlib.util.module_from_spec(spec); spec.loader.exec_module(bj)

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

CLEAN = {
    "power": {"ok": True, "ever": False, "summary": "power clean since boot",
              "rate": {"pct": 0.0, "samples": 500, "live": 0}},
    "path": {"reachable": True, "loss_pct": 0.0, "min_ms": 5.0, "avg_ms": 7.0,
             "max_ms": 10.0, "mdev_ms": 1.5},
    "rate_mismatch": None, "pipeline_rate": "48000", "return_cmdline_found": True,
    "concealment": False, "resampling": False, "mesh_path": "direct",
    "config": {"saved": True, "state": "known-good", "drift": [], "restorable_count": 0},
    "peer": "100.0.0.1", "fleet_tuning": None,
}
def ev(**over):
    d = json.loads(json.dumps(CLEAN)); d.update(over); return d
def top(e):
    return bj._rank(e)[0]
def culprits(e):
    return [f["culprit"] for f in bj._rank(e)]


print("\nJitter diagnosis")
print("================")

print("\n  ---- each known culprit is recognised ----")

f = top(ev(power={"ok": False, "ever": True, "summary": "browning out",
                  "rate": {"pct": 2.33, "samples": 500, "live": 12}}))
if f["culprit"] == "under-voltage" and f["action"] == "jitter-fix":
    ok("2.33%% brownout -> under-voltage, treated with buffer")
else:
    no("brownout not identified", f)
if "no software fix" in (f.get("note") or "").lower():
    ok("says plainly that no software fix exists for it")
else:
    no("implied under-voltage is fixable in software", f.get("note"))

f = top(ev(rate_mismatch={"device_rate": 44100, "pipeline_rate": 48000, "ts": 1}))
if f["culprit"] == "sample-rate mismatch" and f["action"] == "restart":
    ok("rate mismatch -> restart, not a buffer change")
else:
    no("rate mismatch misdiagnosed", f)

f = top(ev(concealment=True))
if f["culprit"].startswith("opus concealment") and f["action"] == "deploy-script":
    ok("concealment ON -> a CODE fix, not a tuning change")
else:
    no("the 2026-08-03 regression would not be caught", f)

f = top(ev(path={"reachable": True, "loss_pct": 0.0, "min_ms": 5, "avg_ms": 20,
                 "max_ms": 90, "mdev_ms": 22.0}))
if f["culprit"] == "network bursts" and f["action"] == "jitter-fix" and f["rung"] == 2:
    ok("22ms spread, no loss -> network bursts, rung 2")
else:
    no("burst jitter misdiagnosed", f)

f = top(ev(path={"reachable": True, "loss_pct": 6.0, "min_ms": 5, "avg_ms": 9,
                 "max_ms": 30, "mdev_ms": 4.0}))
if f["culprit"] == "packet loss" and f["action"] is None:
    ok("real LOSS -> no action offered (a buffer cannot replace missing packets)")
else:
    no("offered a buffer for packet loss", f)

f = top(ev(path={"reachable": False}))
if f["culprit"] == "presenter unreachable":
    ok("unreachable presenter -> not called jitter at all")
else:
    no("down return path misreported as jitter", f)

if "relayed mesh path" in culprits(ev(mesh_path="relay")):
    ok("relayed mesh path is flagged")
else:
    no("relay not detected", culprits(ev(mesh_path="relay")))

if "config drift" in culprits(ev(config={"saved": True, "state": "DRIFTED (2)",
        "drift": [1, 2], "restorable_count": 2})):
    ok("config drift is flagged and linked to golden-restore")
else:
    no("drift not surfaced in the diagnosis")

f = [x for x in bj._rank(ev(config={"saved": True, "state": "DRIFTED (1)", "drift": [1],
                                    "restorable_count": 0})) if x["culprit"] == "config drift"][0]
if f["action"] is None and "deploy" in (f["note"] or "").lower():
    ok("drift that needs a deploy offers NO restore button")
else:
    no("offered golden-restore for a drift it cannot fix", f)

print("\n  ---- ordering: the cause with no software fix comes first ----")
c = culprits(ev(power={"ok": False, "ever": True, "summary": "browning out",
                       "rate": {"pct": 3.0, "samples": 500, "live": 15}},
                path={"reachable": True, "loss_pct": 0.0, "min_ms": 5, "avg_ms": 20,
                      "max_ms": 90, "mdev_ms": 22.0}))
if c and c[0] == "under-voltage":
    ok("brownout outranks network bursts when both are present")
else:
    no("would send the operator chasing the network again", c)

print("\n  ---- a clean board still gets a usable answer ----")
f = top(ev())
if f["culprit"] == "nothing measurable" and f["action"] == "jitter-fix":
    ok("everything clean -> still offers the buffer, and says instruments can read clean")
else:
    no("clean measurement left the operator with nothing", f)
if "audible" in f["detail"]:
    ok("acknowledges audible-but-unmeasurable, which happened on the previous card")
else:
    no("dismisses what cannot be measured", f["detail"])

print("\n  ---- the ladder ----")
tmp = tempfile.mkdtemp()
bj.PRESENTER_TUNE = os.path.join(tmp, "presenter-tuning.json")
r1 = bj.fix(1)
if r1["ok"] and r1["video_interrupted"] is False:
    ok("rung 1 does NOT interrupt video")
else:
    no("rung 1 claimed to be safe but is not", r1)
saved = json.load(open(bj.PRESENTER_TUNE))
if saved["jitter_ms"] == 400 and saved["rung"] == 1 and saved.get("ts"):
    ok("rung 1 publishes 400ms with a timestamp for the app to notice")
else:
    no("tuning file is not what the app expects", saved)
if oct(os.stat(bj.PRESENTER_TUNE).st_mode)[-3:] == "644":
    ok("tuning file is world-readable (bridge-web serves it as 'pi')")
else:
    no("app could never read it", oct(os.stat(bj.PRESENTER_TUNE).st_mode))

r2 = bj.fix(2)
if json.load(open(bj.PRESENTER_TUNE))["jitter_ms"] == 600 and not r2["video_interrupted"]:
    ok("rung 2 is more buffer, still no video interruption")
else:
    no("rung 2 wrong", r2)

if bj.fix(9)["ok"] is False:
    ok("an invalid rung is refused")
else:
    no("accepted a rung that does not exist")

r = bj.reset()
back = json.load(open(bj.PRESENTER_TUNE))
if back["jitter_ms"] == 250 and back["rung"] == 0:
    ok("reset publishes an explicit default rather than deleting the file")
else:
    no("reset would be invisible to the app", back)
# Deleting the file would leave the app on whatever it last applied, with no way to know the
# override was withdrawn — the reset would silently not happen.
if os.path.exists(bj.PRESENTER_TUNE):
    ok("reset leaves an explicit marker (a vanished file is not an instruction)")
else:
    no("reset deleted the file; the app would keep the old value")

print("\n  ---- negative control ----")
if bj._rank(ev(concealment=False)) and "opus concealment is ON" not in culprits(ev()):
    ok("does not invent a culprit that is not in the evidence")
else:
    no("reports culprits with no supporting evidence")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
