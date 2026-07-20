#!/usr/bin/env python3
"""Render the NetBridge in-camera status frame (YUYV 320x180) — walkthrough J2 card.
Deterministic PIL renderer; replaces the quoting-fragile gst-launch textoverlay chain."""
import hashlib, os, subprocess, sys

W, H = 640, 360
OUT = "/etc/bridge/idle-frame.raw"
SIG = "/etc/bridge/idle-frame.sig"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8).stdout.strip()
    except Exception:
        return ""

serial = "unknown"
for l in open("/proc/cpuinfo"):
    if l.startswith("Serial"):
        serial = l.split(":")[1].strip()
code = hashlib.sha256(serial.encode()).hexdigest()[:4].upper()

wifi_ok = "Connected" in sh("/usr/sbin/iw dev wlan0 link")
net_ok = sh("nmcli networking connectivity check") == "full"

def usb_ok():
    # Real UDC state, not a painted-on checkmark. "configured" means a host has
    # enumerated us — i.e. the USB cable really is in the meeting laptop. Any
    # other state (not attached / powered / addressed) means it is not, and the
    # receiver needs to see that instead of a green tick that is always green.
    try:
        for d in os.listdir("/sys/class/udc"):
            with open("/sys/class/udc/%s/state" % d) as f:
                if f.read().strip() == "configured":
                    return True
    except Exception:
        pass
    return False

usb = usb_ok()
t = lambda ok: "✓" if ok else "✗"

# ── EARLY EXIT ────────────────────────────────────────────────────────────────
# This runs on a 20s timer forever, but the status almost never changes. Building
# the image and converting it to YUYV costs ~2.5s of CPU in pure Python — a ~13%
# permanent duty cycle on one core, which is not free on a Pi whose supply already
# browns out under load. So: fingerprint the inputs and bail out BEFORE importing
# PIL or touching a pixel. Unchanged runs now cost ~0.2s (just the probes).
sig = "v2|%s|%d%d%d" % (code, wifi_ok, net_ok, usb)
if os.path.exists(OUT):
    try:
        if open(SIG).read().strip() == sig:
            print("idle frame unchanged (NetBridge %s, wifi=%s net=%s usb=%s)"
                  % (code, t(wifi_ok), t(net_ok), t(usb)))
            sys.exit(0)
    except Exception:
        pass    # no/unreadable signature -> fall through and render once

from PIL import Image, ImageDraw, ImageFont

# Headline must not contradict the ticks. "Bridge X is online" under three red
# crosses is worse than useless to the person standing at the meeting laptop —
# it is the bridge's only voice, so it should say what is actually wrong and what
# to do about it (walkthrough J2: "the bridge talks to them through Zoom").
if wifi_ok and net_ok:
    headline = "Bridge %s is online" % code
    subline = "Waiting for your presenter to go live…"
elif wifi_ok and not net_ok:
    headline = "Bridge %s has no internet" % code
    subline = "On Wi-Fi, but it can't reach the internet"
else:
    headline = "Bridge %s needs Wi-Fi" % code
    subline = "Join “BridgeSetup-BRIDGE-%s” from your phone to set it up" % code

img = Image.new("RGB", (W, H), (17, 24, 21))          # --screen #111815
d = ImageDraw.Draw(img)
brand   = ImageFont.truetype(FONT, 18)
title   = ImageFont.truetype(FONTB, 32)
msg     = ImageFont.truetype(FONT, 22)
checks  = ImageFont.truetype(FONT, 22)

def fit(path, size, text, max_w=W - 32):
    """Shrink until the line actually fits the frame. Without this, any message
    longer than the original ones runs off both edges — which is exactly what the
    'needs Wi-Fi' line did, and it is unreadable precisely when it matters most."""
    f = ImageFont.truetype(path, size)
    while size > 11 and d.textlength(text, font=f) > max_w:
        size -= 1
        f = ImageFont.truetype(path, size)
    return f

def center(y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)

center(76,  "N E T B R I D G E",                       brand,  (147, 162, 154))  # --screen-muted
ticks = "%s Wi-Fi     %s Internet     %s USB host" % (t(wifi_ok), t(net_ok), t(usb))
center(124, headline, fit(FONTB, 32, headline), (232, 239, 234))   # --screen-ink
center(176, subline,  fit(FONT,  22, subline),  (155, 180, 173))
center(236, ticks,    fit(FONT,  22, ticks),
            (79, 190, 132) if (wifi_ok and net_ok and usb) else (198, 57, 44))

# RGB -> YUYV (BT.601), 2 pixels per macropixel
px = img.load()
buf = bytearray(W * H * 2)
i = 0
for y in range(H):
    for x in range(0, W, 2):
        def yuv(p):
            r, g, b = p
            return (int(0.299*r + 0.587*g + 0.114*b),
                    int(-0.169*r - 0.331*g + 0.5*b + 128),
                    int(0.5*r - 0.419*g - 0.081*b + 128))
        y0, u0, v0 = yuv(px[x, y]); y1, u1, v1 = yuv(px[x+1, y])
        clamp = lambda v: max(0, min(255, v))
        buf[i:i+4] = bytes([clamp(y0), clamp((u0+u1)//2), clamp(y1), clamp((v0+v1)//2)])
        i += 4
# The uvc pump re-reads this file whenever its mtime changes, so write atomically
# via rename — otherwise the pump can read a half-written frame and show a torn
# card. The byte-compare below is a second-line guard behind the signature check
# above: it catches a stale signature (e.g. after a font or layout change) without
# bumping mtime and making the pump reload for nothing.
try:
    if open(OUT, "rb").read() == bytes(buf):
        open(SIG, "w").write(sig)
        print("idle frame unchanged (NetBridge %s, wifi=%s net=%s usb=%s)"
              % (code, t(wifi_ok), t(net_ok), t(usb)))
        sys.exit(0)
except Exception:
    pass

tmp = OUT + ".tmp"
with open(tmp, "wb") as f:
    f.write(buf)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, OUT)
# Signature written only AFTER the frame is safely in place, so a crash mid-render
# can never leave a signature claiming we rendered something we didn't.
open(SIG, "w").write(sig)
print("idle frame rendered (NetBridge %s, wifi=%s net=%s usb=%s, %d bytes)"
      % (code, t(wifi_ok), t(net_ok), t(usb), len(buf)))
