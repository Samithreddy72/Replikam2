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

    # Bitrate sanity: 640x360@20 must not be starved. The old rule of thumb ("4x the pixels needs
    # ~4x the 400k", i.e. >= 1280k) was replaced by measurement on 2026-09-24: through the bridge's
    # own pipeline, H.264 Baseline 640x360 at 800k scored SSIM 0.957/0.972 on two scenes against
    # 0.926/0.947 for the old 320x180 at 400k - clearly better, not worse - with less Pi CPU.
    # Nothing below 700k was measured, so that is the floor.
    mb = re.search(r'^STREAM_BITRATE\s*=\s*"(\d+)k"', APP, re.M)
    if not mb:
        no("STREAM_BITRATE not declared")
    else:
        kb = int(mb.group(1))
        if kb >= 700:
            ok("bitrate %dk is at or above the measured floor for 640x360 (700k)" % kb)
        else:
            no("bitrate %dk is below the measured floor for 640x360 (700k)" % kb,
               "4x the pixels at the old bitrate looks worse, not better")

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

print("\n  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
