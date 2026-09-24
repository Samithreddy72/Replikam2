#!/usr/bin/env python3
"""Artifact identity, and the encoder/descriptor agreement.

Two separate lessons from the 26 Aug audit, both of the same shape: two numbers that were
supposed to describe the same thing were never compared to each other.

  1. THE UPDATER compared version STRINGS. Three different binaries were all stamped 1.1.9,
     so it could not tell them apart and could never deliver a same-version rebuild.
  2. THE ENCODER produced 320x180 into a USB gadget advertising 640x360. Half the resolution
     the transport already carried, given away for nothing, because nobody put the two
     constants side by side.

Cross-file consistency is exactly what a human reviewer misses and a test never does.
"""
import hashlib, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = (ROOT / "app/netbridge-source/source_app.py").read_text()
UVC = (ROOT / "pi/scripts/uvc-raw-setup.sh").read_text()
P = F = 0


def ok(m):
    global P
    P += 1
    print("  \033[32mPASS\033[0m  %s" % m)


def no(m, d=""):
    global F
    F += 1
    print("  \033[31mFAIL\033[0m  %s%s" % (m, ("\n        " + d) if d else ""))


print("NetBridge artifact identity + encoder agreement")
print("===============================================\n")

print("  ---- the updater must key on the digest, not the version string ----")

# Strip comments before looking. The first version of this check matched the phrase inside the
# very comment that EXPLAINS the removal -- the same comment-false-positive that had just been
# fixed in tools/fleet-drift-check.sh an hour earlier. Searching source text for a construct is
# only meaningful once the prose is out of the way.
code_only = "\n".join(l for l in APP.splitlines() if not l.lstrip().startswith("#"))
if re.search(r"ver\s*==\s*APP_VERSION\s*:", code_only):
    no("the version-string equality gate is still live",
       "a same-version rebuild can never be delivered while this exists")
else:
    ok("no `ver == APP_VERSION` gate remains in the update path")

body = APP[APP.index("def check_for_update"):]
body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body

if re.search(r"running\s*=\s*_sha256\(", body) and re.search(r"running\s*==\s*want", body):
    ok("the running binary is hashed and compared against the manifest sha256")
else:
    no("update decision must compare the running binary's digest to the manifest")

# Fail-closed: if we cannot hash ourselves we must NOT update. An updater that proceeds when
# it cannot establish identity is worse than one that never runs.
# NB: `[^)]*` cannot match `_sha256(str(exe))` -- the nested call contains a ')'. Match the
# STRUCTURE instead: a try whose body hashes us, and an except that returns None.
m = re.search(r"try:\s*\n\s*running\s*=\s*_sha256\(.*?\)\s*\n\s*except\b.*?\n(?:\s*#.*\n)*\s*return None",
              body, re.S)
if m:
    ok("failing to hash the running binary returns None (fails closed)")
else:
    no("a hash failure must abort the update, not fall through")

# The digest check must happen BEFORE the download, otherwise every poll pulls 53 MB.
i_dig = body.find("running == want")
i_dl = body.find("urlretrieve(\"%s/%s\" % (root, fname)")
if i_dig != -1 and (i_dl == -1 or i_dig < i_dl):
    ok("the digest check runs before the artifact is downloaded")
else:
    no("digest check must precede the download, or every poll re-fetches the whole binary")

# The signature check must still gate everything.
if body.find("_verify_sig") != -1 and body.find("_verify_sig") < i_dig:
    ok("the manifest signature is still verified before any of this is trusted")
else:
    no("signature verification must come first")

if re.search(r'if\s+_sha256\(str\(blob\)\)\s*!=\s*want', body):
    ok("the DOWNLOADED artifact is still verified against the manifest digest")
else:
    no("downloaded bytes must be re-checked against the manifest")

print("\n  ---- the encoder must agree with the USB descriptor ----")

mw = re.search(r"^STREAM_W,\s*STREAM_H\s*=\s*(\d+),\s*(\d+)", APP, re.M)
if not mw:
    no("STREAM_W/STREAM_H not found in the app")
else:
    ew, eh = int(mw.group(1)), int(mw.group(2))
    ok("encoder target is declared as named constants (%dx%d), not buried literals" % (ew, eh))

    # what the gadget actually advertises: create_frame <fn> <w> <h> uncompressed u
    mg = re.search(r"create_frame\s+\$?\w+\s+(\d+)\s+(\d+)\s+uncompressed", UVC)
    if not mg:
        no("could not read the UVC frame size from uvc-raw-setup.sh")
    else:
        gw, gh = int(mg.group(1)), int(mg.group(2))
        if (ew, eh) == (gw, gh):
            ok("encoder %dx%d MATCHES the advertised UVC frame %dx%d" % (ew, eh, gw, gh))
        else:
            no("encoder %dx%d does not match the UVC frame %dx%d" % (ew, eh, gw, gh),
               "the laptop will scale; one of these two numbers is wrong")

    if "scale=%d:%d" in APP or ("scale=%d:%d,format=nv12" % (ew, eh)) in APP:
        ok("the ffmpeg filter is built from the constants, not a hard-coded string")
    else:
        no("scale filter should be derived from STREAM_W/STREAM_H")

    # Bitrate sanity: the frame must not be starved. Judged per pixel, from measurement
    # (2026-09-24, H.264 Baseline through the bridge's own pipeline, two scenes):
    #   640x360 @800k = 0.174 bit/px, SSIM 0.972/0.957   480x270 @600k = 0.231, SSIM 0.961/0.941
    #   480x270 @500k = 0.193,        SSIM 0.960/0.938   (old 320x180 @400k = 0.347 but soft: 0.940/0.917)
    # The lowest point judged acceptable was 640x360 at 700k = 0.152 bit/px, so the floor is 0.15.
    # (It replaced a "4x the pixels needs 4x the bits" rule of thumb that measurement contradicted.)
    mb = re.search(r'^STREAM_BITRATE\s*=\s*"(\d+)k"', APP, re.M)
    if not mb:
        no("STREAM_BITRATE not declared")
    else:
        kb = int(mb.group(1))
        mfi = re.search(r"dwFrameInterval\s*\n(\d+)\s*\nEOF", UVC)
        gfps = round(1e7 / int(mfi.group(1))) if mfi else 20
        bpp = kb * 1000.0 / (ew * eh * gfps)
        if bpp >= 0.15:
            ok("bitrate %dk = %.3f bit/px at %dx%d@%d, at or above the measured floor (0.15)" % (kb, bpp, ew, eh, gfps))
        else:
            no("bitrate %dk = %.3f bit/px at %dx%d@%d is below the measured floor (0.15)" % (kb, bpp, ew, eh, gfps),
               "the picture will be starved at this frame size")

    # The sender's frame rate must equal the gadget's. A mismatch is judder by construction: the
    # bridge's videorate duplicates or drops frames to reach the advertised rate (2026-09-24: the Mac
    # camera captures 30, the app sent 20 - two kept of every three, 33/67 ms apart).
    mf = re.search(r"^STREAM_FPS\s*=\s*(\d+)", APP, re.M)
    ms = re.search(r"def start\(self, pi_host, video_idx, audio_idx, fps=(\w+)", APP)
    if not (mf and mfi):
        no("could not read STREAM_FPS from the app or dwFrameInterval from uvc-raw-setup.sh")
    else:
        afps = int(mf.group(1))
        (ok if afps == gfps else no)("sender fps %d %s the gadget's advertised %d fps"
                                     % (afps, "MATCHES" if afps == gfps else "does not match", gfps))
        (ok if ms and ms.group(1) == "STREAM_FPS" else no)(
            "Session.start defaults to STREAM_FPS, not a literal (%s)" % (ms.group(1) if ms else "?"))

print("\n  ---- USB bandwidth must stay inside what the bus can carry ----")
# Pi 4 OTG is USB 2.0. The gadget streams UNCOMPRESSED YUY2 (2 bytes/pixel).
mg = re.search(r"create_frame\s+\$?\w+\s+(\d+)\s+(\d+)\s+uncompressed", UVC)
mi = re.search(r"^(\d+)$", UVC[UVC.find("dwFrameInterval"):], re.M)
if mg and mi:
    gw, gh = int(mg.group(1)), int(mg.group(2))
    fps = round(1e7 / int(mi.group(1)))          # 100ns units
    mbps = gw * gh * 2 * fps * 8 / 1e6
    if mbps < 480 * 0.5:
        ok("%dx%d@%dfps uncompressed = %.0f Mbps, comfortably inside USB 2.0" % (gw, gh, fps, mbps))
    else:
        no("%dx%d@%dfps uncompressed = %.0f Mbps — too close to the 480 Mbps bus" % (gw, gh, fps, mbps),
           "this needs a compressed (MJPEG) UVC format, not a bigger uncompressed frame")
else:
    no("could not compute the gadget's USB bandwidth from the descriptor")

print("\n  ---- the camera stream must fit in ONE isochronous packet per microframe ----")
# The lesson of 2026-09-24: bandwidth was never the limit, the packet MODE was. At 640x360 the
# stream needed streaming_maxpacket 2048 = "high-bandwidth" isochronous (two packets per 125 us
# microframe); the Pi 4's dwc2 missed slots there ("VS request completed with status -61", 14-16/s)
# and the meeting laptop saw choppy video in every app while everything upstream was clean.
mp = re.search(r"echo\s+(\d+)\s*>\s*functions/\$FUNCTION/streaming_maxpacket", UVC)
if not (mg and mi and mp):
    no("could not read frame size, rate and streaming_maxpacket from uvc-raw-setup.sh")
else:
    maxpacket = int(mp.group(1))
    need = gw * gh * 2 * fps                      # bytes per second, YUY2
    ceiling = min(maxpacket, 1024) * 8000         # ONE packet per microframe carries at most 1024 bytes
    if maxpacket > 1024:
        no("streaming_maxpacket %d = high-bandwidth isochronous (more than one packet per microframe)" % maxpacket,
           "the mode where the Pi's USB controller missed slots; keep it at 1024 or less")
    else:
        ok("streaming_maxpacket %d: one packet per microframe (no high-bandwidth mode)" % maxpacket)
    if need <= ceiling * 0.8:
        ok("%dx%d@%dfps needs %.1f MB/s of a %.1f MB/s single-packet ceiling (%.0f%% - headroom kept)"
           % (gw, gh, fps, need / 1e6, ceiling / 1e6, 100.0 * need / ceiling))
    else:
        no("%dx%d@%dfps needs %.1f MB/s of a %.1f MB/s single-packet ceiling (%.0f%%)"
           % (gw, gh, fps, need / 1e6, ceiling / 1e6, 100.0 * need / ceiling),
           "too close to (or over) what one packet per microframe carries")

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
