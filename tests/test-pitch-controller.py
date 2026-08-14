#!/usr/bin/env python3
"""Does the USB audio pitch controller measure the right thing, and stay sane?

WHY THIS EXISTS
---------------
This daemon writes a value that changes how fast another computer sends us audio. Getting it
wrong does not fail loudly — it quietly makes the audio worse, which is the failure this
whole investigation has been chasing. So it gets tested before it ever runs on hardware.

The first version of the controller servoed on `avail` (ring fill) toward a target of 1920
frames. Real data from the card killed it:

    avail : 864      avail_max : 960

`avail` never exceeds one period, because alsasrc drains a period at a time. The target was
unreachable, the error would have stayed permanently negative, and the controller would have
pinned the host at +0.5% forever while believing it was correcting. That bug was found by
looking at a status file, not by reasoning — hence these tests.

  python3 tests/test-pitch-controller.py
"""
import importlib.util, pathlib, sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-pitch.py"
spec = importlib.util.spec_from_file_location("bp", SRC)
bp = importlib.util.module_from_spec(spec); spec.loader.exec_module(bp)

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %s" % (got,))


def simulate(true_rate_hz, seconds=60, nominal=48000):
    """Run the controller's own arithmetic over a simulated stream consuming at true_rate_hz.
    Returns the ppm it would report once the window has filled."""
    hist = []
    appl = 0
    pitch = bp.NOMINAL
    last_ppm = None
    for t in range(seconds):
        appl += true_rate_hz                      # one second of consumption
        hist.append((float(t), appl))
        while len(hist) > 2 and (t - hist[0][0]) > bp.WINDOW_S:
            hist.pop(0)
        span = t - hist[0][0]
        if span >= bp.WINDOW_S * 0.8:
            rate = (appl - hist[0][1]) / span
            ppm = int(round((rate / nominal - 1.0) * 1e6))
            ppm = max(-bp.MAX_PPM, min(bp.MAX_PPM, ppm))
            want = max(bp.PITCH_MIN, min(bp.PITCH_MAX, bp.NOMINAL + ppm))
            if want > pitch + bp.MAX_STEP: want = pitch + bp.MAX_STEP
            elif want < pitch - bp.MAX_STEP: want = pitch - bp.MAX_STEP
            pitch = want
            last_ppm = pitch - bp.NOMINAL
    return last_ppm


print("\nUSB audio pitch controller")
print("==========================")
print("  window=%.0fs  max=%dppm  step=%dppm  clamp=%d..%d"
      % (bp.WINDOW_S, bp.MAX_PPM, bp.MAX_STEP, bp.PITCH_MIN, bp.PITCH_MAX))

print("\n  ---- it measures the right quantity ----")
p = simulate(48000)
if p == 0:
    ok("a perfectly nominal clock -> 0 ppm correction (does not fidget)")
else:
    no("nominal clock produced a correction", p)

# Consuming SLOWER than nominal means the host is sending more than we take: the ring fills,
# and the host must be told to slow down (negative ppm).
#
# Expected values are derived from the INTEGER rate actually simulated, not from the ppm that
# was asked for. int(48000*(1-50e-6)) is 47997, which is -62.5ppm — an earlier version of
# this test asserted "about -50" and failed a controller that was reporting the truth.
for label, rate in (("slower", int(48000 * (1 - 50e-6))),
                    ("faster", int(48000 * (1 + 50e-6)))):
    want = (rate / 48000.0 - 1.0) * 1e6
    got = simulate(rate)
    if got is not None and abs(got - want) <= 2:
        ok("consuming %s: real %+0.1f ppm -> reports %+d ppm" % (label, want, got))
    else:
        no("misreported a %s clock (wanted ~%+0.1f)" % (label, want), got)

print("\n  ---- it refuses to believe nonsense ----")
p = simulate(int(48000 * 1.5), seconds=120)      # 500,000 ppm — impossible for a clock
if p is not None and p <= bp.MAX_PPM:
    ok("a 50%% rate error is clamped to %+d ppm, not obeyed" % p)
else:
    no("obeyed an impossible measurement", p)

p = simulate(int(48000 * 0.5), seconds=120)
if p is not None and p >= -bp.MAX_PPM:
    ok("a half-speed reading is clamped to %+d ppm" % p)
else:
    no("obeyed an impossible measurement", p)

print("\n  ---- it moves gently ----")
# From nominal, how long to reach a 1000ppm correction at MAX_STEP per second?
steps = bp.MAX_PPM / bp.MAX_STEP
if steps >= 3:
    ok("a full-scale correction takes >= %.0f updates, never one lurch" % steps)
else:
    no("slew limit too loose — the host's clock could be yanked", steps)

print("\n  ---- the kernel's own clamp is respected ----")
if bp.PITCH_MIN == 750000 and bp.PITCH_MAX == 1005000:
    ok("clamp matches u_audio: (1000-FBACK_SLOW_MAX)*1000 .. (1000+fb_max)*1000")
else:
    no("clamp does not match the kernel's", (bp.PITCH_MIN, bp.PITCH_MAX))
if bp.NOMINAL + bp.MAX_PPM <= bp.PITCH_MAX:
    ok("our own +%dppm limit sits inside the kernel's +%dppm ceiling"
       % (bp.MAX_PPM, bp.PITCH_MAX - bp.NOMINAL))
else:
    no("we would ask for more than the kernel accepts — silently clipped",
       (bp.NOMINAL + bp.MAX_PPM, bp.PITCH_MAX))

print("\n  ---- the bug that was caught by looking at real data ----")
# avail_max on the card is 960: one period. Any fill-level target above that is unreachable.
if not hasattr(bp, "TARGET_FRAMES"):
    ok("no ring-fill target remains — the controller measures a slope, not a level")
else:
    no("still servoing on ring fill; avail never exceeds one period (960) on this hardware")

print("\n  ---- negative control ----")
if simulate(48000) == 0 and simulate(int(48000*(1+200e-6))) != 0:
    ok("the simulation can tell a drifting clock from a perfect one")
else:
    no("the test cannot distinguish drift — it proves nothing")

print("\n  ---- every name it uses at runtime actually exists ----")
# The suite above tested the ARITHMETIC and never executed main(), so a stale reference in a
# code path that only runs in async mode survived a rewrite: TARGET_FRAMES and GAIN were
# deleted but still named in a log line just past the async check. On a card shipping
# c_sync=adaptive main() returns before reaching it, so nothing failed — until someone used
# gadget-tune:async, at which point the daemon would die with NameError on startup.
#
# Executing main() here would need a real ALSA card, so check statically instead: every
# global a function loads must be defined somewhere. That catches the whole class, not just
# the two names that happened to be wrong.
import ast, builtins
tree = ast.parse(SRC.read_text())
known = set(dir(bp)) | set(dir(builtins))
undefined = []
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
    local = {a.arg for a in fn.args.args}
    if fn.args.vararg:  local.add(fn.args.vararg.arg)
    if fn.args.kwarg:   local.add(fn.args.kwarg.arg)
    for n in ast.walk(fn):
        if isinstance(n, (ast.Assign, ast.For, ast.With, ast.comprehension)):
            for t in ast.walk(n):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    local.add(t.id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
           and n.id not in known and n.id not in local:
            undefined.append("%s() line %d: %s" % (fn.name, n.lineno, n.id))
if undefined:
    no("names used but never defined — this crashes the moment that path runs", undefined)
else:
    ok("no function references a name that does not exist")

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
