#!/usr/bin/env python3
"""Is a "known-good" baseline actually known to be good?

WHY THIS EXISTS
---------------
On 2026-08-24 a `golden-save` was queued by accident while probing which commands the backend
accepted. It succeeded. The fleet then displayed "known-good" for a state nobody had listened
to, and it stayed that way for a day.

That matters more than it sounds. Everything downstream is measured against that record - drift
detection, `golden-restore`, and the operator's question "why is this worse than yesterday?" -
so an unverified baseline is worse than having none at all, because it looks like knowledge.

A device can check that USB is configured, that services are running and that the board is not
browning out. It cannot check the only thing that actually matters: whether the audio SOUNDED
right. That is why operator confirmation is recorded as its own field and never inferred from
green checks.

  python3 tests/test-golden-baseline.py
"""
import importlib.util, json, os, pathlib, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "pi" / "scripts" / "bridge-golden.py"
spec = importlib.util.spec_from_file_location("bg", SRC)
bg = importlib.util.module_from_spec(spec); spec.loader.exec_module(bg)

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

tmp = tempfile.mkdtemp()
bg.GOLDEN = os.path.join(tmp, "golden.json")
HEALTHY = {"reachable": True, "usb_ok": True, "services_ok": True,
           "power_ok": True, "brownout_live": False, "settling": False,
           "usb": "USB_CONNECTED_HEALTHY", "image_version": "2.0.0-test"}

print("\nGolden baseline — is it actually known good?")
print("============================================")

print("\n  ---- a baseline records who stood behind it ----")
bg._live_health = lambda: dict(HEALTHY)
bg.load = lambda: None
bg.collect = lambda: {"restorable": {}, "fingerprint": {}}
p = bg.save("first", by="samith@example.com", confirmed=True,
            verified={"video": True, "audio-to-room": True})
for f in ("saved_by", "operator_confirmed", "verified", "health_at_save"):
    if f in p: ok("records %s" % f)
    else: no("baseline does not record %s" % f)
if p.get("operator_confirmed") is True and p.get("saved_by") == "samith@example.com":
    ok("a confirmed save is attributed to the person who confirmed it")
else:
    no("confirmation or attribution lost", p)

print("\n  ---- an accidental save cannot replace a verified one ----")
bg.load = lambda: {"saved_at": int(time.time()), "saved_by": "samith@example.com",
                   "operator_confirmed": True}
r = bg.save("accident", by="a-script", confirmed=False)
if r.get("ok") is False and r.get("error") == "refused":
    ok("unconfirmed save over a confirmed baseline is REFUSED")
else:
    no("the exact 2026-08-24 accident would still succeed", r)
if "confirmed by samith@example.com" in (r.get("detail") or ""):
    ok("the refusal names who confirmed the existing one, and when")
else:
    no("refusal gives the operator nothing to act on", r.get("detail"))

print("\n  ---- but confirming is never blocked ----")
r = bg.save("verified now", by="samith@example.com", confirmed=True)
if r.get("ok") is not False:
    ok("a CONFIRMED save always proceeds — improving knowledge is not destructive")
else:
    no("refuses a legitimate confirmed save", r)
r = bg.save("deliberate", by="samith@example.com", confirmed=False, force=True)
if r.get("ok") is not False and r.get("forced") is True:
    ok("force still works, and is recorded as forced")
else:
    no("no deliberate override, or the override is not recorded", r)

print("\n  ---- the device refuses to baseline a session that is visibly broken ----")
bg.load = lambda: None
for bad, why in (({"usb_ok": False, "usb": "USB_DISCONNECTED"}, "USB is not connected"),
                 ({"services_ok": False}, "not every media service"),
                 ({"settling": True}, "post-flash work"),
                 ({"brownout_live": True}, "browning out right now")):
    h = dict(HEALTHY); h.update(bad)
    bg._live_health = lambda h=h: h
    r = bg.save("x", by="op", confirmed=True)
    if r.get("ok") is False and any(why in s for s in r.get("problems", [])):
        ok("refuses while %s" % why)
    else:
        no("would baseline a session where %s" % why, r)

print("\n  ---- health is asked of bridge-web, not re-derived ----")
# Two implementations of "is the USB healthy" drift apart, and the point of a baseline is that
# it agrees with what the fleet reports.
src = SRC.read_text()
if "127.0.0.1:8080/api/status" in src:
    ok("reuses the bridge's own status rather than duplicating the logic")
else:
    no("re-derives health locally; the two answers will diverge")
if "cannot judge the one thing that matters most" in src or "Only a person can" in src:
    ok("documents that the device cannot judge whether it SOUNDED right")
else:
    no("does not distinguish machine-checkable from human-checkable")

print("\n  ---- the whole chain carries the confirmation ----")
agent = (ROOT / "pi" / "scripts" / "bridge-agent.py").read_text()
panel = (ROOT / "control-plane" / "panel-dist" / "index.html").read_text()
if '"--confirmed"' in agent and "a.get(\"confirmed\")" in agent:
    ok("the agent forwards confirmation to the device")
else:
    no("confirmation is dropped between the fleet and the device")
if "confirmed: heard" in panel:
    ok("the panel sends what the operator actually answered")
else:
    no("the panel does not send the confirmation")
if "Cancel = save it unconfirmed" in panel:
    ok("declining to confirm still saves — recorded as unconfirmed, not blocked")
else:
    no("an operator who cannot verify has no honest option")

print("\n  ---- negative control ----")
bg._live_health = lambda: dict(HEALTHY); bg.load = lambda: None
a = bg.save("n1", by="op", confirmed=True).get("operator_confirmed")
b = bg.save("n2", by="op", confirmed=False).get("operator_confirmed")
if a is True and b is False:
    ok("confirmed and unconfirmed saves are distinguishable")
else:
    no("the flag does not survive; this test proves nothing", (a, b))

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
