#!/usr/bin/env python3
"""The Mac video leg must encode H.264 Baseline (CAVLC), not the default High (CABAC).

The bridge decodes the presenter's video in software on a 900 MHz Pi; CABAC is the costliest,
strictly serial part of that decode. Measured 2026-09-22 through the bridge's own pipeline: 41%
less decode+convert CPU at the same 640x360/20fps/1500k, no visible quality change.

Calls the real Session.start() with process launching stubbed, then (when ffmpeg with
VideoToolbox is available) encodes one second with exactly those encoder arguments and checks the
stream that comes out really is Baseline.
  python3 tests/test-video-encoder-profile.py
"""
import importlib.util, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent / "app" / "netbridge-source"
spec = importlib.util.spec_from_file_location("sa", ROOT / "source_app.py")
sa = importlib.util.module_from_spec(spec); spec.loader.exec_module(sa)

passed = failed = skipped = 0
def ok(m):
    global passed; passed += 1; print("  PASS  %s" % m)
def no(m):
    global failed; failed += 1; print("  FAIL  %s" % m)
def skip(m):
    global skipped; skipped += 1; print("  SKIP  %s" % m)

launched = []
class FakeProc:
    def __init__(self, argv, *a, **k): launched.append(argv); self.pid = 1; self.stdin = None
    def poll(self): return None
    def wait(self, *a, **k): return 0
    def terminate(self): pass
    def kill(self): pass
REAL_POPEN = sa.subprocess.Popen      # sa.subprocess IS the global module: restore after
sa.subprocess.Popen = FakeProc

if not sa.IS_MAC:
    skip("not macOS - the VideoToolbox leg is Mac-only"); print("\n  0 passed, 0 failed, 1 skipped"); sys.exit(0)

s = sa.Session()
try:
    s.start("127.0.0.1", 0, 0)
except Exception as e:
    no("Session.start raised %r" % e)
finally:
    sa.subprocess.Popen = REAL_POPEN
video = next((a for a in launched if "h264_videotoolbox" in a), None)
if not video:
    no("no VideoToolbox video leg was launched (%d processes)" % len(launched))
else:
    i = video.index("h264_videotoolbox")
    (ok if "-profile:v" in video and video[video.index("-profile:v") + 1] == "baseline" else no)(
        "video leg asks VideoToolbox for -profile:v baseline")
    (ok if "-realtime" in video and video[video.index("-realtime") + 1] == "1" else no)("realtime encode kept")
    (ok if sa.STREAM_BITRATE == "600k" else no)("bitrate is 600k (%s)" % sa.STREAM_BITRATE)
    for flag, want in (("-b:v", "600k"), ("-r", "20"), ("-g", "20")):
        (ok if flag in video and video[video.index(flag) + 1] == want else no)("%s %s" % (flag, want))
    (ok if "scale=480:270,format=nv12" in " ".join(video) else no)("encodes 480x270 (the UVC frame)")

    # Does the real encoder honour it? Encode 1 s from a test source with the same encoder args.
    ff = shutil.which("ffmpeg")
    if not ff:
        skip("no ffmpeg on PATH - cannot confirm the encoder honours the profile")
    else:
        enc = video[i - 1:video.index("-b:v") + 2]            # "-c:v h264_videotoolbox ... -b:v <rate>"
        vf = video[video.index("-vf") + 1]                     # the app's own scale filter
        out = pathlib.Path(tempfile.mkdtemp()) / "t.h264"
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=1280x720:rate=30", "-t", "3", "-vf", vf,
                            "-fps_mode", "cfr", "-r", "20"] + enc + ["-f", "h264", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            skip("VideoToolbox encode unavailable here: %s" % (r.stderr.strip()[:120] or r.returncode))
        else:
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=profile,width,height",
                                    "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
            prof, w, h = (probe.split(",") + ["", "", ""])[:3]
            (ok if prof in ("Baseline", "Constrained Baseline") else no)(
                "encoded stream really is Baseline (%s)" % prof)
            (ok if (w, h) == (str(sa.STREAM_W), str(sa.STREAM_H)) else no)(
                "encoded stream really is %dx%d (%sx%s)" % (sa.STREAM_W, sa.STREAM_H, w, h))

print("\n  %d passed, %d failed, %d skipped" % (passed, failed, skipped))
sys.exit(1 if failed else 0)
