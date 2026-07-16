#!/usr/bin/env python3
"""Render the NetBridge in-camera status frame (YUYV 320x180) — walkthrough J2 card.
Deterministic PIL renderer; replaces the quoting-fragile gst-launch textoverlay chain."""
import hashlib, subprocess, sys
from PIL import Image, ImageDraw, ImageFont

W, H = 320, 180
OUT = "/etc/bridge/idle-frame.raw"
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
t = lambda ok: "✓" if ok else "✗"

img = Image.new("RGB", (W, H), (17, 24, 21))          # --screen #111815
d = ImageDraw.Draw(img)
brand   = ImageFont.truetype(FONT, 9)
title   = ImageFont.truetype(FONTB, 16)
msg     = ImageFont.truetype(FONT, 11)
checks  = ImageFont.truetype(FONT, 11)

def center(y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)

center(38,  "N E T B R I D G E",                       brand,  (147, 162, 154))  # --screen-muted
center(62,  "Bridge %s is online" % code,              title,  (232, 239, 234))  # --screen-ink
center(88,  "Waiting for your presenter to go live…", msg, (155, 180, 173))
center(118, "%s Wi-Fi     %s Internet     ✓ USB host" % (t(wifi_ok), t(net_ok)),
            checks, (79, 190, 132))                                              # zcheck green

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
open(OUT, "wb").write(buf)
print("idle frame rendered (NetBridge %s, wifi=%s net=%s, %d bytes)" % (code, t(wifi_ok), t(net_ok), len(buf)))
