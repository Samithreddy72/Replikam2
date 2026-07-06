#!/bin/bash
# Renders the in-camera status frame -> /etc/bridge/idle-frame.raw (YUYV 320x180).
# The video pump shows this whenever no presenter stream has arrived, so the
# meeting laptop sees "bridge online, waiting" instead of black/gray.
export PATH=/usr/local/bin:/usr/bin:/bin
CODE=$(python3 - <<PY
import hashlib
serial="unknown"
for l in open("/proc/cpuinfo"):
    if l.startswith("Serial"): serial=l.split(":")[1].strip()
print("BRIDGE-"+hashlib.sha256(serial.encode()).hexdigest()[:4].upper())
PY
)
mkdir -p /etc/bridge
gst-launch-1.0 -q videotestsrc num-buffers=1 pattern=black \
  ! video/x-raw,width=320,height=180 ! videoconvert \
  ! textoverlay text="RepliKam  ·  ${CODE}" valignment=center halignment=center ypad=28 font-desc="Sans Bold 13" color=0xFFFFFFFF \
  ! textoverlay text="online — waiting for your presenter" valignment=center halignment=center ypad=-10 font-desc="Sans 9" color=0xFF9BB4AD \
  ! videoconvert ! video/x-raw,format=YUY2,width=320,height=180 \
  ! filesink location=/etc/bridge/idle-frame.raw 2>/dev/null
[ "$(stat -c%s /etc/bridge/idle-frame.raw 2>/dev/null)" = "115200" ] && echo "idle frame rendered (${CODE})" || echo "render failed"
