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
    "services": [], "quarantined": [], "udc": "configured", "wifi_dbm": -41,
    "temp": "48'C", "clock_suspect": False,
    "checks": {"return_audio": {"ok": True, "detail": "hw_ptr advancing"}},
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

print("\n  ---- the AUDIO failures this project actually hit ----")
# Every case below is a real incident, not a hypothetical. Silence is not jitter, so these
# must outrank every buffer suggestion: no depth of buffer improves a dead pipeline.

f = top(ev(services=[["bridge-feeder-audio", "failed"]]))
if "presenter voice path is DOWN" in f["culprit"] and f["action"] == "restart":
    ok("feeder-audio dead (the S16LE bug) -> the ROOM hears nothing, restart")
else:
    no("the 2026-08-12 endianness outage would not be named", f)
if "no buffer helps" in f["detail"]:
    ok("says explicitly that this is silence, not jitter")
else:
    no("could still be mistaken for a jitter problem", f["detail"])
if "revert-script" in (f.get("note") or ""):
    ok("points at revert-script if it will not stay up (a broken pipeline, not a service)")
else:
    no("no escalation path for a crash-looping pipeline", f.get("note"))

f = top(ev(services=[["bridge-return-audio", "failed"]]))
if "room audio path is DOWN" in f["culprit"] and f["action"] == "restart":
    ok("return-audio dead (the '! !' bug) -> the PRESENTER hears nothing, restart")
else:
    no("the 2026-08-12 empty-variable outage would not be named", f)

f = top(ev(clock_suspect=True))
if f["culprit"] == "degraded UAC2 audio clock" and f["action"] == "reset-clock":
    ok("crackle -> reset-clock, the ONE cause it is the right answer to")
else:
    no("reset-clock is not wired to its actual cause", f)
if "re-selected" in (f.get("note") or ""):
    ok("states the real cost: the meeting laptop loses camera, mic and speakers")
else:
    no("recommends reset-clock without naming what it breaks", f.get("note"))

f = top(ev(checks={"return_audio": {"ok": False, "detail": "hw_ptr stalled at 0"}}))
if "not playing into NetBridge" in f["culprit"] and f["action"] is None:
    ok("services fine but no frames -> the meeting laptop's OUTPUT setting, no button")
else:
    no("would send an operator to restart things that are already working", f)
if "SPEAKER" in (f.get("note") or "") and "not the microphone" in (f.get("note") or ""):
    ok("names the SPEAKER setting — the direction that was got wrong once before")
else:
    no("ambiguous about which device setting is at fault", f.get("note"))

f = top(ev(udc="not attached"))
if f["culprit"] == "no USB host attached" and f["action"] is None:
    ok("USB not configured -> charge-only cable, honestly no fleet fix")
else:
    no("offered a remote fix for an unplugged cable", f)

f = top(ev(quarantined=["bridge-return-audio.sh"]))
if f["culprit"] == "deployed code is not running" and f["action"] == "unquarantine":
    ok("auto-rollback parked the override -> unquarantine")
else:
    no("the code being debugged is not the code running, and nothing says so", f)

print("\n  ---- silence outranks jitter ----")
c = culprits(ev(services=[["bridge-return-audio", "failed"]],
                path={"reachable": True, "loss_pct": 0.0, "min_ms": 5, "avg_ms": 22,
                      "max_ms": 95, "mdev_ms": 26.0}))
if c[0] == "room audio path is DOWN":
    ok("a dead audio path outranks a noisy network")
else:
    no("would tune a buffer while the pipeline is dead", c)

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

print("\n  ---- who owns the buffer: the sentry must never overrule a human ----")
# Plan D lets jitter-sentry act DURING a live session, which is the only time it matters.
# That is safe only if it can never undo a deliberate choice. The old sentry's worst moment
# was tearing down a live stream because the network had been pristine for ten minutes —
# unhelpful autonomy is the specific failure mode being guarded here.
bj.PRESENTER_TUNE = os.path.join(tempfile.mkdtemp(), "own.json")
r = bj.fix(1, auto=True, why="loss=0% jitter=30ms")
if r["ok"] and (bj._read_tuning() or {}).get("by") == "auto":
    ok("the sentry can apply rung 1 on its own")
else:
    no("sentry cannot act", r)
r = bj.fix(1, auto=True)
if r.get("skipped"):
    ok("re-applying the same rung is skipped (no gap in room audio every 20s)")
else:
    no("sentry would rebuild the pipeline on every loop", r)
r = bj.fix(2, auto=True)
if r["ok"] and (bj._read_tuning() or {}).get("rung") == 2:
    ok("the sentry may ESCALATE its own rung when trouble persists")
else:
    no("sentry cannot escalate", r)

bj.fix(1)                                  # operator takes ownership, deliberately lower
r = bj.fix(2, auto=True)
if not r["ok"] and "operator" in (r.get("skipped") or ""):
    ok("the sentry refuses to override a rung an operator set by hand")
else:
    no("a machine reading a ping overrode a human who could HEAR the problem", r)
r = bj.reset(auto=True)
if r.get("skipped") and (bj._read_tuning() or {}).get("rung") == 1:
    ok("the sentry withdraws only its OWN changes, never the operator's")
else:
    no("sentry handed back a buffer a human asked for", (r, bj._read_tuning()))
r = bj.reset()
if (bj._read_tuning() or {}).get("rung") == 0:
    ok("the operator can always take it back")
else:
    no("operator lost control of their own setting", bj._read_tuning())

bj.fix(1, auto=True)
r = bj.reset(auto=True)
if r["ok"] and (bj._read_tuning() or {}).get("rung") == 0:
    ok("on recovery the sentry does withdraw what it applied itself")
else:
    no("auto changes are never cleaned up", r)

print("\n  ---- negative control ----")
if bj._rank(ev(concealment=False)) and "opus concealment is ON" not in culprits(ev()):
    ok("does not invent a culprit that is not in the evidence")
else:
    no("reports culprits with no supporting evidence")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
