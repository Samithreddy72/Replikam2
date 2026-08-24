#!/usr/bin/env python3
"""Does every service that is supposed to run actually get enabled?

WHY THIS EXISTS
---------------
`bridge-crackle-sentry` shipped in every image for weeks and was never enabled. It is the
service that writes the crackle verdict that bridge-web exposes as `clock_suspect` — the
signal the jitter diagnosis reads to decide whether to recommend `reset-clock`. With the
service disabled that flag could only ever be False, so the one culprit `reset-clock` is the
correct answer to was undetectable, and nothing anywhere said so.

Installing a unit and enabling it are separate steps in ci-build-image.sh, and a unit that
ships without being enabled is silent: no error, no log, nothing missing that anyone would
notice. Exactly the kind of gap that only shows up as "why did the diagnosis never catch it".

  python3 tests/test-units-enabled.py
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "factory" / "ci-build-image.sh"
UNITS = ROOT / "pi" / "systemd"

# Deliberately NOT enabled — started by hand or by another unit. Each needs a reason, so that
# adding something here is a decision rather than a way to silence the test.
ON_DEMAND = {
    "bridge-feeder":      "superseded by bridge-feeder-net/-audio; kept for manual use",
    "bridge-soak":        "a soak test, run by hand",
    "bridge-testpattern": "explicitly disabled at build time; used for bring-up",
    "bridge-update":      "invoked by the update command, not at boot",
    # Turned OFF again on 2026-08-14. Both ship so they can be switched on for a specific
    # experiment; neither runs by default. See the dedicated section below for why.
    "bridge-crackle-sentry": "unproven detector; enable deliberately, never by default",
    "bridge-pitch":          "inert unless c_sync=async; enable only for that experiment",
}

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))

print("\nsystemd units: shipped vs enabled")
print("=================================")

src = BUILD.read_text()
m = re.search(r"for u in (.*?); do\n\s*systemctl enable", src, re.S)
if not m:
    no("could not find the enable loop in ci-build-image.sh — this check is BLIND")
    print("\n  %d passed, %d failed\n" % (passed, failed)); sys.exit(1)
ok("parsed the enable loop")

enabled = {t.replace(".timer", "") for t in m.group(1).replace("\\\n", " ").split()}
shipped = {p.stem for p in UNITS.glob("*.service")}
ok("found %d shipped units, %d enabled" % (len(shipped), len(enabled & shipped)))

print("\n  ---- every shipped unit is either enabled or explained ----")
unexplained = sorted(u for u in shipped if u not in enabled and u not in ON_DEMAND)
if not unexplained:
    ok("no unit ships silently disabled")
else:
    no("shipped but never enabled, and not listed as on-demand", unexplained)

print("\n  ---- the ones this test was written for ----")
# Compare on the same normalisation the set was built with — .timer is stripped there, so
# looking up "bridge-agent.timer" verbatim reports a false failure on a unit that IS enabled.
def is_enabled(u):
    return u.replace(".timer", "") in enabled

for u, why in (("jitter-sentry", "measures the path and adapts"),
               ("flight-recorder", "the power black box"),
               ("bridge-web", "serves /api/status to the app and the fleet"),
               ("bridge-agent.timer", "the only way the fleet reaches this device")):
    if is_enabled(u):
        ok("%-24s enabled  (%s)" % (u, why))
    else:
        no("%s is NOT enabled — %s" % (u, why))

print("\n  ---- installed, but deliberately NOT enabled ----")
# These two were enabled on 13 Aug and turned back off on 14 Aug. Neither had ever been shown
# to help, and bridge-pitch does nothing whatsoever while the gadget is in adaptive mode -
# which is what the image ships. During an audio fault we still cannot explain, every extra
# service running in a live session is another variable. They stay installed so either can be
# switched on deliberately for an experiment; they must not switch themselves on.
for u, why in (("bridge-crackle-sentry", "never caught a real crackle; unproven"),
               ("bridge-pitch", "inert unless c_sync=async, which is not the shipped default")):
    if not (UNITS / (u + ".service")).exists():
        no("%s should still SHIP, just not run" % u)
    elif is_enabled(u):
        no("%s is enabled again — it must be off by default (%s)" % (u, why))
    else:
        ok("%-24s installed, not enabled  (%s)" % (u, why))

print("\n  ---- everything enabled actually exists ----")
ghosts = sorted(u for u in enabled if u not in shipped
                and not (UNITS / (u + ".timer")).exists())
if not ghosts:
    ok("no unit is enabled that was never shipped")
else:
    no("enable list names units with no unit file", ghosts)

print("\n  ---- negative control ----")
if "definitely-not-a-unit" not in enabled:
    ok("a made-up unit is correctly absent")
else:
    no("the parser is matching things that are not there")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
