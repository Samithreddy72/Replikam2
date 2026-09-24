#!/usr/bin/env python3
"""Does the bridge know the difference between video ARRIVING and video being WATCHABLE?

WHY THIS EXISTS
---------------
`video_arriving` measured whether the feeder was burning CPU. That answers "is something
happening" and says nothing about whether the meeting room can see a usable picture: a stream
that has collapsed to two frames a second burns CPU in exactly the same way as a healthy one.
The 2026-08-25 audit could state that video ARRIVED and could not state that it was
ACCEPTABLE, and those are the two different claims a customer cares about.

Bytes written to /dev/video40 is the honest measure. The format is raw YUY2, so the size per
frame is exactly known and frames-per-second follows by division - measured, not estimated.

  python3 tests/test-video-health.py
"""
import importlib.util, pathlib, sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "pi" / "scripts" / "bridge-web.py"
spec = importlib.util.spec_from_file_location("bw", SRC)
bw = importlib.util.module_from_spec(spec); spec.loader.exec_module(bw)
src = SRC.read_text()

passed = failed = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m, got=None):
    global failed; failed += 1; print("  FAIL  %s" % m)
    if got is not None: print("          %r" % (got,))

print("\nVideo health — arriving vs watchable")
print("====================================")

print("\n  ---- frames, not effort ----")
if "def video_throughput" in src:
    ok("the bridge measures frames delivered to the gadget")
else:
    no("still only measures whether the feeder is busy")
if "wchar" in src:
    ok("counts bytes actually written to /dev/video40")
else:
    no("no throughput source — CPU is not a picture")

print("\n  ---- a rate needs two samples ----")
bw._VIDEO_SEEN.clear()
if bw.video_throughput(None, 1.0) is None:
    ok("no pid -> None, not a fabricated zero")
else:
    no("invents a rate with nothing to measure")
bw._VIDEO_SEEN.clear()
bw._video_bytes = lambda pid: 0
bw.video_throughput("1", 100.0)
FRAME = bw._video_frame_bytes()      # bytes per frame as the bridge computes it (424x240 YUY2 fallback off-Pi)
bw._video_bytes = lambda pid: FRAME * 20 * 2        # two seconds of 20fps
r = bw.video_throughput("1", 102.0)
if r is not None and abs(r - 20.0) < 0.5:
    ok("20fps of bytes over 2s reads as %.1f fps" % r)
else:
    no("throughput arithmetic is wrong", r)

print("\n  ---- a degraded stream is called degraded ----")
bw._VIDEO_SEEN.clear()
bw._video_bytes = lambda pid: 0
bw.video_throughput("1", 200.0)
bw._video_bytes = lambda pid: FRAME * 4            # 4 frames in 2s = 2fps
slow = bw.video_throughput("1", 202.0)
want = bw._expected_fps()
if slow is not None and slow < want * 0.66:
    ok("2fps against an expected %d falls below the degraded threshold" % want)
else:
    no("a collapsed stream would still be reported as healthy", (slow, want))
if "DEGRADED: the room is seeing a stuttering picture" in src:
    ok("the operator is told what the room actually sees")
else:
    no("no operator-facing wording for a degraded picture")

print("\n  ---- the expectation is read, not hard-coded ----")
# A health check comparing against a constant nobody maintains silently rots when the pipeline
# changes. The expected rate comes from the setup script that configures the gadget.
# Behaviour, not a source grep: until 2026-09-24 _expected_fps read the descriptor script, found no
# pattern it recognised, and returned its fallback whatever the gadget advertised.
ROOT = pathlib.Path(__file__).resolve().parent.parent
real_uvc = (ROOT / "pi" / "scripts" / "uvc-raw-setup.sh").read_text()
real_read = bw.read
def fake_files(files):
    bw.read = lambda path: files.get(path, "")
fake_files({"/home/pi/uvc-raw-setup.sh": real_uvc})
got = bw._expected_fps()
(ok if got == 30 else no)("reads 30 fps from the shipped descriptor script (%s)" % got)
fake_files({"/home/pi/uvc-raw-setup.sh": real_uvc.replace("\n333333\n", "\n500000\n")})
got = bw._expected_fps()
(ok if got == 20 else no)("follows the descriptor: a 500000 interval reads as 20 fps (%s)" % got)
fake_files({"/usr/local/bin/bridge-gadget-setup.sh": 'set-caps /dev/video40 "YUYV:424x240@25/1"'})
got = bw._expected_fps()
(ok if got == 25 else no)("without the descriptor, falls back to the loopback caps (%s)" % got)
fake_files({})
got = bw._expected_fps()
(ok if got == 30 else no)("nothing readable -> the documented default, 30 (%s)" % got)
bw.read = real_read
if bw._video_frame_bytes() == 424 * 240 * 2:
    ok("frame size derives from the configured format (YUY2, 2 bytes/pixel)")
else:
    no("frame size is wrong", bw._video_frame_bytes())

print("\n  ---- a stale sample is not evidence about now ----")
bw._VIDEO_SEEN.clear()
bw._video_bytes = lambda pid: 0
bw.video_throughput("1", 300.0)
bw._video_bytes = lambda pid: FRAME * 1000
if bw.video_throughput("1", 300.0 + 3600) is None:
    ok("an hour-old sample is discarded rather than averaged into a rate")
else:
    no("would report an hour-old average as the current frame rate")

print("\n  ---- negative control ----")
bw._VIDEO_SEEN.clear()
bw._video_bytes = lambda pid: 0
bw.video_throughput("1", 400.0)
bw._video_bytes = lambda pid: FRAME * 40
fast = bw.video_throughput("1", 402.0)
if fast and slow and fast > slow:
    ok("healthy and degraded streams produce different numbers")
else:
    no("returns the same regardless of input; proves nothing", (fast, slow))

print("\n  %d passed, %d failed\n" % (passed, failed))
sys.exit(1 if failed else 0)
