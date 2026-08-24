#!/usr/bin/env python3
"""Can a bridge tell you it is still busy with post-flash work?

WHY THIS EXISTS
---------------
On 2026-08-24 an operator flashed a card, went live straight away, and heard loud periodic
bursts - clearly worse than the jitter we had been chasing - which then stopped on their own
after a few minutes.

Nothing was wrong. Flashing rewrites the entire disk including /data, so the /data/.expanded
guard is wiped and the filesystem expansion runs AGAIN on that boot. `resize2fs` is heavily
IO-bound and `ssh-keygen -A` regenerates every host key, and both were competing with a
real-time media pipeline on the same four cores.

Every metric looked healthy the whole time, because none of them answered "this machine is
busy with something that finishes by itself". An operator hearing bursts with all-green
checks will start changing settings that were never the problem - which is exactly the loop
this project keeps falling into.

  python3 tests/test-settling.py
"""
import importlib.util, pathlib, sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-web.py"
spec = importlib.util.spec_from_file_location("bw", SRC)
bw = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(bw)
except Exception as e:
    print("  FAIL  bridge-web.py does not import: %s" % e); sys.exit(1)

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

print("\nA settling bridge says so")
print("=========================")

src = SRC.read_text()

print("\n  ---- it reports the state at all ----")
if hasattr(bw, "settling"):
    ok("settling() exists")
else:
    no("no way to distinguish a settling bridge from a broken one")
if 'd["settling"] = settling()' in src:
    ok("exposed in /api/status")
else:
    no("computed but never reported")
if "STILL SETTLING" in src:
    ok("also surfaced in /api/checks, which is what the app actually polls")
else:
    no("only in /api/status — the operator hearing bursts would never see it")

print("\n  ---- it costs nothing once the machine is settled ----")
# It is polled every 5-10s forever; spawning systemctl four times per call for a condition
# that can only be true just after a boot would be a permanent tax for a transient answer.
import unittest.mock as mock
calls = []
with mock.patch.object(bw, "sh", lambda *a, **k: calls.append(a) or ""):
    with mock.patch("builtins.open", mock.mock_open(read_data="99999.0 99999.0")):
        r = bw.settling()
if r is False and not calls:
    ok("an hour-old bridge returns False without spawning a single process")
else:
    no("still shelling out on a long-settled machine", (r, len(calls)))

print("\n  ---- it recognises the units that only run after a flash ----")
for u in ("bridge-firstboot", "bridge-regen-hostkeys"):
    if u in getattr(bw, "FIRSTBOOT_UNITS", ()):
        ok("%s is watched" % u)
    else:
        no("%s not watched — its work would go unannounced" % u)

print("\n  ---- the reason the guard cannot be trusted across a flash ----")
# /data/.expanded is meant to make the expansion once-per-card. Flashing writes the whole
# disk image, /data included, so the guard file goes with it and the work repeats.
if "flash" in (bw.settling.__doc__ or "").lower():
    ok("documented as a per-FLASH event, not a per-card one")
else:
    no("docstring does not explain why this recurs")

print("\n  ---- negative control ----")
with mock.patch.object(bw, "sh", lambda *a, **k: "starting"):
    with mock.patch("builtins.open", mock.mock_open(read_data="30.0 30.0")):
        early = bw.settling()
if early is True:
    ok("a freshly booted machine mid-startup reports True")
else:
    no("cannot detect settling even when systemd says 'starting'", early)

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
