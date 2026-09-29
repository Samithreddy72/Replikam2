#!/usr/bin/env python3
"""When something breaks, is the operator sent to the RIGHT end?

WHY THIS EXISTS
---------------
Twice during the 2026-08-24/25 audit the system detected a fault correctly and named the wrong
device. That is the most expensive failure this project has, because the operator has no reason
to doubt it and spends their five minutes at the far end of a healthy link.

  A wedged macOS camera was reported as "check the bridge". avfoundation had handed out the
  device and was delivering nothing; ffmpeg sat there alive, poll() returned None, and the
  supervisor concluded the local end was fine. A process being alive is not proof that a
  camera is producing frames.

  An unplugged USB cable was reported as `udc: not attached`, which in the fleet reads like
  the bridge is offline. It cost twenty minutes; the bridge was healthy the whole time.

  python3 tests/test-fault-attribution.py
"""
import ast, importlib.util, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "netbridge-source" / "source_app.py"
BW  = ROOT / "pi" / "scripts" / "bridge-web.py"

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

spec = importlib.util.spec_from_file_location("bw", BW)
bw = importlib.util.module_from_spec(spec); spec.loader.exec_module(bw)
app_src = APP.read_text()

print("\nFault attribution — which end is at fault?")
print("=========================================")

print("\n  ---- USB: five situations, not one ----")
G = ("uac2.usb0 uvc.0", True, True)      # a healthy gadget
cases = [
    ("configured",   "USB_CONFIGURED_UNVERIFIED", False),
    ("suspended",    "USB_HOST_SUSPENDED",       True),
    ("addressed",    "USB_HOST_NOT_ENUMERATING", True),
    ("powered",      "USB_HOST_NOT_ENUMERATING", True),
    ("not attached", "USB_DISCONNECTED",         False),
]
for state, want, want_certain in cases:
    d, certain, detail = bw.usb_diagnosis(state, *G)
    if d == want and certain == want_certain:
        ok("%-14s -> %-26s certain=%s" % (state, d, certain))
    else:
        no("%s misclassified" % state, (d, certain))

print("\n  ---- a bridge-side gadget failure is NOT blamed on the cable ----")
d, certain, detail = bw.usb_diagnosis("not attached", "", False, False)
if d == "USB_GADGET_FAULT" and "bridge fault" in detail:
    ok("missing uac2/uvc -> USB_GADGET_FAULT, named as a bridge fault")
else:
    no("a gadget that never built is reported as a cable problem", (d, detail))

print("\n  ---- ambiguity is admitted, not papered over ----")
# A charge-only cable and an unplugged cable are indistinguishable without a VBUS sense line.
# Claiming certainty here would send someone to buy a cable when the laptop was simply off.
d, certain, detail = bw.usb_diagnosis("not attached", *G)
if certain is False:
    ok("'not attached' reports certain=False")
else:
    no("claims certainty about a state the hardware cannot resolve")
if "cannot tell them apart" in detail and "cable" in detail:
    ok("the detail names the possibilities and says which to check first")
else:
    no("the operator is not told what the ambiguity is", detail)
d, certain, _ = bw.usb_diagnosis("something-new", *G)
if d == "UNKNOWN" and certain is False:
    ok("an unrecognised kernel state is UNKNOWN, not guessed")
else:
    no("invents a diagnosis for a state it does not know", d)

print("\n  ---- the gadget check runs AFTER the fields it depends on ----")
# Placed above them, every reading said USB_GADGET_FAULT because it read a dict key that had
# not been filled in yet. Found by running it, not by reading it.
src = BW.read_text()
# Find the CALL, not the definition. Searching for "usb_diagnosis(state" matched the `def`
# hundreds of lines earlier and reported a regression in correct code - the same too-eager
# match that produced two false failures in the app audit.
i_fn  = src.find('d["functions"]')
i_uac = src.find('d["uac2"] =')
i_use = src.find("= usb_diagnosis(state")
if 0 < i_fn < i_use and 0 < i_uac < i_use:
    ok("classification happens after functions/video40/uac2 are known")
else:
    no("ordering regression: the classifier reads fields that are not set yet")

print("\n  ---- Mac: alive is not the same as working ----")
if "def leg_cpu_rate" in app_src:
    ok("the app can measure what a leg is DOING, not just whether it exists")
else:
    no("no throughput signal — a wedged camera still looks healthy")
if "LOCAL_CAMERA_FAULT" in app_src and "fix-camera-macos.sh" in app_src:
    ok("a wedged camera is named LOCAL_CAMERA_FAULT with the repair command")
else:
    no("still sends the operator to the bridge for a local camera fault")
if "LOCAL_MIC_FAULT" in app_src:
    ok("an idle microphone is distinguished from a network fault")
else:
    no("no local mic classification")

print("\n  ---- the threshold is a floor, not a fitted constant ----")
# 26.81/81s healthy vs 0.23/56s wedged is ~80x. A threshold sitting between two measurements
# from one machine on one day would not survive a faster Mac or a lighter stream.
m = [n for n in ast.walk(ast.parse(app_src))
     if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "CPU_FLOOR" for t in n.targets)]
if m:
    val = ast.literal_eval(m[0].value)
    if val <= 0.05:
        ok("CPU_FLOOR=%s sits far below any real encode (~0.33) and above idle (~0.004)" % val)
    else:
        no("floor is close enough to real work to misfire on a slow machine", val)
else:
    no("no CPU_FLOOR constant")
if "first sample: a rate needs two" in app_src:
    ok("a rate is never computed from a single sample")
else:
    no("could report a rate from one reading, which is not a rate")

print("\n  ---- negative control ----")
a = bw.usb_diagnosis("configured", *G)[0]
b = bw.usb_diagnosis("not attached", *G)[0]
if a != b:
    ok("the classifier distinguishes healthy from disconnected — it can fail")
else:
    no("returns the same answer regardless of input; proves nothing")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
