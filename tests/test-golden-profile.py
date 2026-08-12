#!/usr/bin/env python3
"""Golden Profile: does it detect drift, and is it honest about what it can fix?

The dangerous failure here is not missing a drift — it is claiming a drift is fixable when
it is not. c_srate is written at every boot by uvc-raw-setup.sh on the READ-ONLY root, so
poking configfs would be undone at the next reboot while looking, to the operator, exactly
like a successful repair. An operator on a live call needs "restore" to mean something.

  python3 tests/test-golden-profile.py
"""
import importlib.util, json, os, pathlib, shutil, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "pi" / "scripts" / "bridge-golden.py"

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          got: %r" % (got,))


def fresh_root():
    """A fake filesystem so this runs anywhere, with no Pi and no root."""
    R = tempfile.mkdtemp(prefix="golden-")
    for d in ("data", "etc/default", "etc/bridge", "usr/local/bin", "home/pi"):
        os.makedirs(os.path.join(R, d), exist_ok=True)
    w = lambda p, s: open(os.path.join(R, p), "w").write(s)
    w("etc/default/bridge-net", "NET_VIDEO_LATENCY=300\nNET_AUDIO_LATENCY=300\n")
    w("etc/default/bridge-return-audio", "RETURN_DEST_IP=10.0.0.1\nRETURN_AEC=0\n")
    w("etc/default/bridge-return-tune", 'RETURN_SRC_PROPS=""\nRETURN_PRE_RESAMPLE=""\n')
    w("etc/bridge/version", "2.0.0-test\n")
    for f in ("bridge-feeder-audio.sh", "bridge-return-audio.sh", "bridge-feeder-net.sh"):
        w("usr/local/bin/" + f, "#!/bin/bash\n# %s v1\n" % f)
    w("home/pi/uvc-raw-setup.sh", "#!/bin/bash\necho 48000,44100,32000 > c_srate\n")

    spec = importlib.util.spec_from_file_location("g_%s" % os.path.basename(R), SRC)
    g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
    g.GOLDEN = R + "/data/golden-profile.json"
    g.NET_DEF = R + "/etc/default/bridge-net"
    g.TUNE_DEF = R + "/etc/default/bridge-return-tune"
    g.PEER_DEF = R + "/etc/default/bridge-return-audio"
    g.CODE_FILES = tuple(R + p for p in (
        "/usr/local/bin/bridge-feeder-audio.sh", "/usr/local/bin/bridge-return-audio.sh",
        "/usr/local/bin/bridge-feeder-net.sh", "/home/pi/uvc-raw-setup.sh"))
    g._restarted = []
    import subprocess
    g.subprocess = type("S", (), {"run": staticmethod(
        lambda cmd, **k: g._restarted.append(cmd[-1]))})()
    # The gadget lives in configfs, which does not exist off a Pi. Default to the good value;
    # individual tests override it.
    g._gadget_rates = lambda: {"c_srate": "48000,44100,32000", "p_srate": "48000,44100,32000"}
    return R, g


print("\nGolden Profile")
print("==============")

# ------------------------------------------------------------------ baseline lifecycle
print("\n  ---- baseline ----")
R, g = fresh_root()
d = g.diff()
if d["saved"] is False and d["state"] == "no baseline" and not d["drift"]:
    ok("no baseline -> says so; does not invent a verdict")
else:
    no("missing baseline mishandled", d)

g.save("verified good")
d = g.diff()
if d["state"] == "known-good" and not d["drift"]:
    ok("straight after save -> known-good, zero drift")
else:
    no("a fresh save should show no drift", d)

# ------------------------------------------------------------------ restorable drift
print("\n  ---- drift that CAN be fixed from the fleet ----")
open(g.NET_DEF, "w").write("NET_VIDEO_LATENCY=200\nNET_AUDIO_LATENCY=200\n")
d = g.diff()
if len(d["drift"]) == 2 and all(x["fixable"] for x in d["drift"]):
    ok("jitter profile change -> 2 drifts, both marked fixable")
else:
    no("jitter profile drift misreported", d["drift"])
if d["restorable_count"] == 2 and d["needs_deploy_count"] == 0:
    ok("counts split correctly (2 restorable, 0 needing deploy)")
else:
    no("bad split", (d["restorable_count"], d["needs_deploy_count"]))

r = g.restore()
if r["ok"] and "net" in r["restored"]:
    ok("restore rewrites the net profile")
else:
    no("restore failed", r)
if r["video_interrupted"] is True:
    ok("restore WARNS that restoring the net profile interrupts video")
else:
    no("restore hid the video interruption from the operator", r)
if "bridge-feeder-net" in r["restarted"] and "bridge-return-audio" not in r["restarted"]:
    ok("restarts only what the changed field requires")
else:
    no("restarted the wrong services", r["restarted"])
if g.diff()["state"] == "known-good":
    ok("after restore -> back to known-good")
else:
    no("restore did not converge", g.diff())

r = g.restore()
if r["ok"] and not r["restored"] and not r["restarted"]:
    ok("restoring an already-good bridge is a no-op (restarts nothing)")
else:
    no("needless restart on a clean bridge", r)

# ------------------------------------------------------------------ the honesty test
print("\n  ---- drift that CANNOT be fixed from the fleet ----")
R2, g2 = fresh_root()
g2.save("verified good")
g2._gadget_rates = lambda: {"c_srate": "48000", "p_srate": "48000,44100,32000"}
d = g2.diff()
csr = [x for x in d["drift"] if x["field"].endswith("c_srate")]
if csr and csr[0]["fixable"] is False:
    ok("c_srate regression is reported as NEEDS DEPLOY, not as fixable")
else:
    no("c_srate drift wrongly advertised as restorable", csr)
if csr and csr[0]["golden"] == "48000,44100,32000" and csr[0]["current"] == "48000":
    ok("shows the exact before/after (the frequencies regression)")
else:
    no("drift row lacks usable detail", csr)

r = g2.restore()
if not r["restarted"]:
    ok("restore does NOT restart anything for an unfixable drift")
else:
    no("restore churned services it could not fix", r)
if any(x["field"].endswith("c_srate") for x in r["still_drifted"]):
    ok("restore reports what it could not fix instead of claiming success")
else:
    no("restore silently ignored the unfixable drift", r)

# A code change must also be report-only.
R3, g3 = fresh_root()
g3.save("verified good")
open(g3.CODE_FILES[0], "w").write("#!/bin/bash\n# tampered\n")
d = g3.diff()
code = [x for x in d["drift"] if "code" in x["field"]]
if code and not code[0]["fixable"]:
    ok("a changed media script is detected and marked NEEDS DEPLOY")
else:
    no("code drift missed or mislabelled", d["drift"])

# ------------------------------------------------------------------ durability
print("\n  ---- durability ----")
R4, g4 = fresh_root()
g4.save("x")
raw = open(g4.GOLDEN).read()
if json.loads(raw) and oct(os.stat(g4.GOLDEN).st_mode)[-3:] == "644":
    ok("baseline is valid JSON and world-readable (bridge-web runs as 'pi')")
else:
    no("baseline unreadable by the web process", oct(os.stat(g4.GOLDEN).st_mode))
open(g4.GOLDEN, "w").write("{ truncated")
d = g4.diff()
if d["saved"] is False:
    ok("a corrupt baseline reads as 'no baseline', not as a crash")
else:
    no("corrupt baseline mishandled", d)

for R_ in (R, R2, R3, R4):
    shutil.rmtree(R_, ignore_errors=True)

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
