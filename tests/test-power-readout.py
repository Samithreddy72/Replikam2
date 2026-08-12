#!/usr/bin/env python3
"""Does the bridge tell the truth about its own power?

WHY THIS EXISTS
---------------
This one function has now produced two production-visible faults, in opposite directions,
and both times the code "looked right":

  2026-08-10  It read the hwmon in0_lcrit_alarm node first. That node is INSTANTANEOUS, so
              it answered 0x0 on nearly every poll — and because it answered, the
              authoritative read was never reached. The panel reported a healthy board while
              the flight recorder was catching live brownouts in 1.3% of sampled seconds.
              Weeks of audio debugging looked straight past the actual cause.

  2026-08-13  bridge-web runs as User=pi, and on the new image `vcgencmd` needs
              /dev/vcio_gencmd, which pi cannot open. It prints "Can't open device file:
              /dev/vcio_gencmd" on stdout and exits 0 — so that sentence was stored in the
              `raw` field, handed to int(), and surfaced as "unreadable". Meanwhile the
              fleet's brownout percentage stayed correct, because it comes from flight.txt.
              Two views of one board disagreeing, with the more prominent one wrong.

Both bugs are about SOURCE SELECTION, not arithmetic, so testing the arithmetic would have
caught neither. These tests fake the sources.

  python3 tests/test-power-readout.py
"""
import importlib.util, pathlib, sys, tempfile, os

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "pi" / "scripts" / "bridge-web.py"

spec = importlib.util.spec_from_file_location("bridge_web", SRC)
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)          # safe: the server only starts under __main__

passed = failed = 0
def ok(msg):
    global passed; passed += 1; print("  PASS  %s" % msg)
def no(msg, got=None):
    global failed; failed += 1; print("  FAIL  %s" % msg)
    if got is not None:
        print("          got: %r" % (got,))

def with_sources(pairs):
    bw.throttle_sources = lambda: list(pairs)
def flight(lines):
    bw._flight_tail = lambda window=500: list(lines)
def no_hwmon():
    bw.undervolt_now = lambda: None

_orig = (bw.throttle_sources, bw._flight_tail, bw.undervolt_now, bw.sh)
def reset():
    bw.throttle_sources, bw._flight_tail, bw.undervolt_now, bw.sh = _orig

print("\nPower readout")
print("=============")

# ---------------------------------------------------------------- the mask validator
print("\n  ---- a reading must look like a reading ----")
for bad in ["Can't open device file: /dev/vcio_gencmd", "", None, "throttled=", "?",
            "VCHI initialization failed", "0xZZZZ", "not available"]:
    if bw._valid_mask(bad) is None:
        ok("rejected junk: %r" % (bad if bad != "" else "<empty>",))
    else:
        no("accepted junk as a mask: %r" % (bad,), bw._valid_mask(bad))
for good, want in [("0x50000", "0x50000"), ("throttled=0x50000", "0x50000"),
                   ("0x0", "0x0"), ("50000", "0x50000"), (" 0x1 \n", "0x1")]:
    got = bw._valid_mask(good)
    ok("accepted %r -> %s" % (good, got)) if got == want else no("mis-parsed %r" % good, got)

# ---------------------------------------------------------------- source selection
print("\n  ---- source selection ----")
no_hwmon()

# The exact 2026-08-13 failure: vcgencmd blind, flight recorder holding the truth.
with_sources([("flight", 0x50000)])
flight(["20:52:39 up=1 udc=configured thr=0x50000 pull=0"] * 100)
st = bw.power_state()
if st["ok"] is False and st["ever"] and st["raw"] == "0x50000":
    ok("vcgencmd blind + flight recorder has it -> still reports the brownout")
else:
    no("vcgencmd blind: the fault was lost", st)
if st.get("source") == "flight":
    ok("reports WHICH source answered ('flight')")
else:
    no("no provenance on the reading", st.get("source"))

# The 2026-08-10 failure: an instantaneous source saying 0x0 must not mask the history.
with_sources([("sysfs", 0x0), ("flight", 0x50000)])
st = bw.power_state()
if st["ever"] and st["ok"] is False:
    ok("a source reading 0x0 cannot erase another source's sticky bits")
else:
    no("0x0 from one source masked a real fault", st)

# No source at all is an ABSENCE, not a clean board.
with_sources([])
flight([])
st = bw.power_state()
if st["ok"] is None and st["raw"] is None:
    ok("no source available -> ok=None, raw=None (absence, never 'clean')")
else:
    no("missing data was reported as a verdict", st)
if "unreadable" in st["summary"] and "vcio" not in st["summary"]:
    ok("summary says unreadable without echoing a command's error text")
else:
    no("error text leaked into the summary", st["summary"])

# A genuinely clean board must still be able to say so.
with_sources([("sysfs", 0x0)])
flight(["20:52:39 up=1 udc=configured thr=0x0 pull=0"] * 100)
st = bw.power_state()
if st["ok"] is True and not st["ever"]:
    ok("a genuinely clean board still reports clean")
else:
    no("clean board misreported", st)

# ---------------------------------------------------------------- the rate
print("\n  ---- brownout rate ----")
flight(["t up=1 udc=configured thr=0x%x pull=0" % (0x50001 if i < 5 else 0x50000)
        for i in range(100)])
r = bw.brownout_rate()
if r and r["samples"] == 100 and r["live"] == 5 and r["pct"] == 5.0:
    ok("counts live brownout seconds: 5/100 = 5.0%")
else:
    no("rate miscounted", r)

flight([])
if bw.brownout_rate() is None:
    ok("no recorder data -> None, not 0% (absence != clean)")
else:
    no("empty recorder reported as a rate", bw.brownout_rate())

# Above 2% the verdict must be actionable, because that is the audible threshold measured
# on this hardware (0.50% clean by ear, 2.33% audible).
with_sources([("flight", 0x50000)])
flight(["t thr=0x50001"] * 3 + ["t thr=0x50000"] * 97)
st = bw.power_state()
if st["ok"] is False and "audible" in st["summary"]:
    ok("3%% brownout rate -> 'enough to be audible'")
else:
    no("high rate did not produce an actionable summary", st["summary"])

# ---------------------------------------------------------------- real-world regression
print("\n  ---- the actual failing output from 2026-08-13 ----")
reset()
bw.sh = lambda cmd: "Can't open device file: /dev/vcio_gencmd"   # what pi really got
bw._flight_tail = lambda window=500: ["20:52:41 up=1699 udc=configured thr=0x50000 pull=0"] * 50
bw.undervolt_now = lambda: None
st = bw.power_state()
if st["raw"] == "0x50000" and st["ok"] is False:
    ok("end to end: the live API now reports 0x50000 where it reported 'unreadable'")
else:
    no("the original bug is not fixed", st)
if st["raw"] and "vcio" not in str(st["raw"]):
    ok("error text never appears in the raw field")
else:
    no("error text still in raw", st["raw"])
reset()

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
