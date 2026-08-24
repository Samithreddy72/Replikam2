#!/usr/bin/env python3
"""Can the fleet tell a live session from a bridge that is merely switched on?

WHY THIS EXISTS
---------------
On 2026-08-24 the fleet panel showed "Scine Test — live" while the presenter app was stopped,
no encoder was running on the Mac, and no RTP was being sent by anyone. The bridge was
reporting streams video/voice/return all true.

The old test was:

    "video": svc.get("bridge-feeder-net") == "active" and attached

Those services are enabled at boot and sit on their UDP sockets forever, so `active` is true
on an idle bridge. `attached` only means a USB cable is plugged into the meeting laptop. So
the condition reduced to "powered, with a cable in it" — an indicator that cannot be false
when the device is on, which is no indicator at all. An admin reading that panel would think
a call was in progress.

Liveness is now a DELTA on a counter that only moves when real media flows.

  python3 tests/test-stream-liveness.py
"""
import importlib.util, pathlib, sys, types

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-web.py"

# bridge-web imports cleanly but binding is behind __main__, so importing is safe.
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

print("\nStream liveness — is it real, or just powered on?")
print("=================================================")

print("\n  ---- the 2026-08-24 case ----")
bw._STREAM_SEEN.clear()
# An idle bridge: the feeder exists and burns no CPU, so the counter never advances.
first  = bw._stream_live("video", 5000, 100.0)
second = bw._stream_live("video", 5000, 105.0)
third  = bw._stream_live("video", 5000, 110.0)
if not (first or second or third):
    ok("a counter that never moves is never reported live, however long we poll")
else:
    no("an idle bridge still reports live", (first, second, third))

print("\n  ---- a real session ----")
bw._STREAM_SEEN.clear()
bw._stream_live("voice", 5000, 100.0)                      # first poll: nothing to compare
if bw._stream_live("voice", 5040, 105.0):
    ok("a counter that advances between polls reports live")
else:
    no("real traffic was not recognised")

print("\n  ---- the session ends ----")
# Feeder stops burning CPU the moment the presenter disconnects; the flag must drop.
if not bw._stream_live("voice", 5040, 110.0):
    ok("when the counter stops advancing, live goes false on the very next poll")
else:
    no("liveness latches on and never clears — the original complaint")

print("\n  ---- first poll after a restart ----")
bw._STREAM_SEEN.clear()
if not bw._stream_live("return", 12345, 100.0):
    ok("no previous sample -> False (one poll late, rather than wrong forever)")
else:
    no("claims live with nothing to compare against")

print("\n  ---- a stale cache proves nothing ----")
bw._STREAM_SEEN.clear()
bw._stream_live("video", 1000, 100.0)
if not bw._stream_live("video", 9999, 100.0 + 3600):
    ok("an hour-old sample is not evidence about now, even though the counter grew")
else:
    no("treated an hour-old delta as current traffic")

print("\n  ---- a stream that disappears ----")
bw._STREAM_SEEN.clear()
bw._stream_live("video", 1000, 100.0)
if not bw._stream_live("video", None, 105.0):
    ok("a feeder that is gone entirely reads as not live, not as an error")
else:
    no("a missing counter was treated as live")

print("\n  ---- an idle feeder is not a live one ----")
# Measured on hardware: decoding real RTP burns ~14 CPU ticks/second; an idle GStreamer
# feeder still wakes occasionally and manages about 0.1. `value > old` counted that as live,
# so after a session ended the voice flag flickered true/false for minutes on a bridge with
# nothing arriving at it.
bw._STREAM_SEEN.clear()
bw._stream_live("voice", 1000, 100.0)
if not bw._stream_live("voice", 1001, 110.0):          # 1 tick in 10s = 0.1/s
    ok("an idle feeder ticking over does NOT read as live")
else:
    no("any advance still counts — the flag will flicker after a session ends")

bw._STREAM_SEEN.clear()
bw._stream_live("voice", 1000, 100.0)
if bw._stream_live("voice", 1028, 102.0):              # 28 ticks in 2s = 14/s
    ok("a feeder actually decoding RTP reads as live")
else:
    no("real traffic is now being rejected — the floor is too high")

print("\n  ---- the return stream is exempt, and must be ----")
# Its counter is a capture pointer in frames at the sample rate, and it keeps advancing
# whenever the meeting laptop plays into the bridge - which correctly continues after the
# presenter has gone.
bw._STREAM_SEEN.clear()
bw._stream_live("return", 0, 100.0)
if bw._stream_live("return", 48000, 101.0):
    ok("return audio still reports live from the capture pointer")
else:
    no("the CPU floor was wrongly applied to a frame counter")

print("\n  ---- negative control ----")
bw._STREAM_SEEN.clear()
bw._stream_live("x", 10, 1.0)
moving = bw._stream_live("x", 20, 2.0)
bw._STREAM_SEEN.clear()
bw._stream_live("y", 10, 1.0)
still = bw._stream_live("y", 10, 2.0)
if moving and not still:
    ok("the test distinguishes a moving counter from a still one")
else:
    no("cannot tell movement from stillness; proves nothing", (moving, still))

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
