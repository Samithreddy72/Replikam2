#!/bin/bash
# ONE measurement, run identically at every rate, so the numbers are comparable.
#
# The point is not "does it look healthy" — every stage has looked healthy for days. The
# point is that the SAME instrument reads the SAME signal at 48k, 44.1k and 32k, so any
# difference between rates is attributable to the rate and nothing else.
#
# What it counts, and why each one matters:
#   dropouts        exact-zero runs >1ms — audible gaps the transport metrics cannot see
#   discontinuities big sample-to-sample jumps — the "click/bar" signature at followed rates
#   peak/clip       whether the app's volume=2.0 is driving music past full scale
#   rms drift       level walking over the window = a resampler correcting continuously
#
#   bash return-rate-probe.sh <label> [seconds]
set -uo pipefail
LABEL="${1:?usage: return-rate-probe.sh <label> [seconds]}"
SECS="${2:-15}"
OUT="${SCRATCH:-/tmp}/rateprobe"; mkdir -p "$OUT"
RAW="$OUT/$LABEL.raw"

GST="$(ls -d /var/folders/*/*/T/_MEI*/gst/gst-launch-1.0 2>/dev/null | head -1)"
[ -x "$GST" ] || GST="$(command -v gst-launch-1.0)"
[ -x "$GST" ] || { echo "❌ no gst-launch-1.0" >&2; exit 1; }
export GST_PLUGIN_PATH="$(dirname "$GST")/plugins" DYLD_LIBRARY_PATH="$(dirname "$GST")"
CAPS='application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000'

# Tap the SAME udp stream the app plays — a second reader does not disturb the first.
rm -f "$RAW"
"$GST" -q udpsrc port=5004 caps="$CAPS" ! rtpjitterbuffer latency=250 ! rtpopusdepay \
  ! opusdec ! audioconvert ! audio/x-raw,format=S16LE,channels=2,rate=48000 \
  ! filesink location="$RAW" >/dev/null 2>&1 &
GP=$!
for i in $(seq 1 "$SECS"); do printf "\r  capturing %s … %d/%ds" "$LABEL" "$i" "$SECS"; sleep 1; done; echo
kill $GP 2>/dev/null; sleep 1

RAW="$RAW" LABEL="$LABEL" python3 - <<'PY'
import os, struct

SR = 48000
raw = open(os.environ["RAW"], "rb").read()
n = len(raw) // 4
if n < SR:
    print("  ❌ too little audio — is the stream live and playing?"); raise SystemExit
s = struct.unpack("<%dh" % (n * 2), raw[:n * 4])
L = s[::2]
dur = n / SR

# exact-zero runs longer than 1ms: a real gap, not a quiet passage
gap = SR // 1000
runs, cur, longest = 0, 0, 0
for v in L:
    if v == 0:
        cur += 1
        if cur == gap: runs += 1
        longest = max(longest, cur)
    else:
        cur = 0

# sample-to-sample jumps beyond a quarter of full scale = the click signature
disc = sum(1 for i in range(1, len(L)) if abs(L[i] - L[i-1]) > 8192)

peak = max(abs(min(L)), abs(max(L)))
clip = sum(1 for v in L if abs(v) >= 32700)

# RMS per second — a level that walks means something is continuously correcting
rms = []
for t in range(int(dur)):
    seg = L[t*SR:(t+1)*SR]
    if seg: rms.append((sum(v*v for v in seg)/len(seg)) ** 0.5)
mean = sum(rms)/len(rms) if rms else 0
cv = (sum((r-mean)**2 for r in rms)/len(rms))**0.5/mean if mean else 0

print(f"\n  ── {os.environ['LABEL']} ── {dur:.1f}s")
print(f"  dropouts >1ms    {runs:5d}   ({runs/dur:.2f}/s)   longest {longest/SR*1000:.1f} ms")
print(f"  discontinuities  {disc:5d}   ({disc/dur:.2f}/s)")
print(f"  peak             {peak/32768*100:5.1f}% FS   clipped {clip} ({clip/len(L)*100:.3f}%)")
print(f"  level stability  cv {cv:.3f}   {'steady' if cv < 0.6 else 'varying (normal for music)'}")
PY
