#!/bin/bash
# REFERENCE A/B — compare what NetBridge DELIVERS against the ORIGINAL file.
#
# Every previous measurement asked "does this stage look healthy in isolation?" and every
# stage said yes. This asks a different question: what actually DIFFERS between the source
# and what arrives? With the original file as ground truth we can answer that exactly.
#
# The specific thing we are hunting is CLOCK DRIFT. The Windows PC's audio clock is not
# exactly 48000 Hz and neither is the Pi's; `c_sync = adaptive` means the gadget corrects
# for the difference continuously. Those corrections are INAUDIBLE on a steady tone (a sine
# is invariant to small time warps) and audible as smear/jitter on music. That is precisely
# why the 1 kHz tone measured pristine while music sounds wrong — the tone could not have
# revealed this even if it were severe.
#
# So we cross-correlate the captured audio against the original in WINDOWS ACROSS TIME. A
# constant offset is just latency and is fine. An offset that WALKS is drift, and its slope
# gives us the clock error in ppm directly.
#
#   bash return-reference-ab.sh <original-audio-file> [seconds]
#
# Play the SAME file on the Windows PC, with the presenter app live, then run this.
set -uo pipefail
ORIG="${1:?usage: $0 <original-audio-file> [seconds]}"
SECS="${2:-45}"
[ -f "$ORIG" ] || { echo "❌ no such file: $ORIG" >&2; exit 1; }

OUT="${SCRATCH:-/tmp}/refab"
mkdir -p "$OUT"
CAPRAW="$OUT/captured.raw"
ORIGRAW="$OUT/original.raw"

# Reuse the bundled GStreamer the app ships with, falling back to a system one.
GST="$(ls -d /var/folders/*/*/T/_MEI*/gst/gst-launch-1.0 2>/dev/null | head -1)"
[ -x "$GST" ] || GST="$(command -v gst-launch-1.0)"
[ -x "$GST" ] || { echo "❌ gst-launch-1.0 not found" >&2; exit 1; }
export GST_PLUGIN_PATH="$(dirname "$GST")/plugins" DYLD_LIBRARY_PATH="$(dirname "$GST")"
CAPS='application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000'

echo "════ decoding the original to raw 48k stereo S16LE ════"
ffmpeg -v error -y -i "$ORIG" -ac 2 -ar 48000 -f s16le "$ORIGRAW" || {
  echo "❌ ffmpeg could not decode the original" >&2; exit 1; }
echo "  ✅ $(( $(stat -f%z "$ORIGRAW") / 4 / 48000 ))s of reference"

echo "════ capturing ${SECS}s from the live return stream (udp:5004) ════"
echo "     (the presenter app must be LIVE and the track playing on Windows)"
pkill -f 'location=.*captured.raw' 2>/dev/null; sleep 1; rm -f "$CAPRAW"
"$GST" -q udpsrc port=5004 caps="$CAPS" ! rtpjitterbuffer latency=250 ! rtpopusdepay \
  ! opusdec ! audioconvert ! audio/x-raw,format=S16LE,channels=2,rate=48000 \
  ! filesink location="$CAPRAW" >/dev/null 2>&1 &
GPID=$!
for i in $(seq 1 "$SECS"); do printf "\r     %d/%ds" "$i" "$SECS"; sleep 1; done; echo
kill $GPID 2>/dev/null; pkill -f 'location=.*captured.raw' 2>/dev/null; sleep 1

BYTES=$(stat -f%z "$CAPRAW" 2>/dev/null || echo 0)
[ "$BYTES" -gt 192000 ] || {
  echo "❌ captured only ${BYTES} bytes — nothing is arriving on 5004."
  echo "   Check: presenter app LIVE, audio playing on Windows, peer set to this Mac."
  exit 1; }
echo "  ✅ captured $(( BYTES / 4 / 48000 ))s"

ORIGRAW="$ORIGRAW" CAPRAW="$CAPRAW" python3 - <<'PY'
import os, struct, math

SR = 48000
def mono(path, limit=None):
    raw = open(path, "rb").read()
    n = len(raw) // 4
    if limit: n = min(n, limit)
    s = struct.unpack("<%dh" % (n * 2), raw[:n * 4])
    # Mid channel; drift affects both identically and this halves the work.
    return [(s[i * 2] + s[i * 2 + 1]) * 0.5 for i in range(n)]

ref = mono(os.environ["ORIGRAW"])
cap = mono(os.environ["CAPRAW"])
print(f"\n  reference {len(ref)/SR:.1f}s   captured {len(cap)/SR:.1f}s")

# ---------- delivered-signal health, measured on the capture alone ----------
clip = sum(1 for v in cap if abs(v) >= 32700)
zrun = zmax = 0
for v in cap:
    if v == 0:
        zrun += 1; zmax = max(zmax, zrun)
    else:
        zrun = 0
print(f"\n  ── delivered signal ──")
print(f"  peak            {max(abs(min(cap)), abs(max(cap)))/32768*100:5.1f}% FS")
print(f"  clipped samples {clip}  ({clip/len(cap)*100:.3f}%)")
print(f"  longest silence {zmax/SR*1000:.1f} ms")

# ---------- envelope, coarse, for alignment ----------
# Full-rate cross-correlation over minutes of audio is far too slow in pure Python. The
# envelope at 100 Hz preserves the timing information we need (we are looking for drift
# measured in milliseconds over tens of seconds, not sample-level phase).
HOP = SR // 100
def envelope(x):
    return [max(abs(v) for v in x[i:i+HOP]) or 1e-9
            for i in range(0, len(x) - HOP, HOP)]
er, ec = envelope(ref), envelope(cap)

def norm(v):
    m = sum(v) / len(v)
    d = math.sqrt(sum((a - m) ** 2 for a in v)) or 1e-9
    return [(a - m) / d for a in v]

def best_offset(a, b, lo, hi):
    """Offset (in envelope hops) of b within a maximising correlation."""
    na, nb = norm(a), norm(b)
    best, bo = -2.0, 0
    for off in range(lo, hi):
        if off < 0 or off + len(nb) > len(na): continue
        c = sum(na[off + i] * nb[i] for i in range(len(nb)))
        if c > best: best, bo = c, off
    return bo, best

WIN = 300          # 3 s of envelope per probe window
SEARCH = 1200      # ±12 s alignment search

# Coarse global alignment using the first window.
g0, gc = best_offset(er, ec[:WIN], 0, min(SEARCH, len(er) - WIN))
print(f"\n  ── alignment ──")
print(f"  initial offset  {g0*10} ms   (correlation {gc:.3f})")
if gc < 0.5:
    print("  ⚠️  weak correlation — is the SAME track playing? Analysis below is unreliable.")

# ---------- THE DRIFT MEASUREMENT ----------
# Probe alignment at several points through the capture. Constant offset = plain latency.
# Offset that walks = the two clocks disagree, and the slope is the error in ppm.
print(f"\n  ── drift (the thing a 1 kHz tone cannot show) ──")
probes = []
step = max(WIN, (len(ec) - WIN) // 6)
for start in range(0, len(ec) - WIN, step):
    lo = g0 + start - 200
    hi = g0 + start + 200
    off, c = best_offset(er, ec[start:start + WIN], lo, hi)
    if c > 0.4:
        drift_ms = (off - (g0 + start)) * 10
        probes.append((start / 100.0, drift_ms, c))
        print(f"  t={start/100:5.1f}s   drift {drift_ms:+7.0f} ms   corr {c:.3f}")

if len(probes) >= 3:
    t0, d0, _ = probes[0]
    t1, d1, _ = probes[-1]
    span = t1 - t0
    slope_ms_per_s = (d1 - d0) / span if span else 0.0
    ppm = slope_ms_per_s * 1000.0
    print(f"\n  slope           {slope_ms_per_s:+.3f} ms/s  =  {ppm:+.0f} ppm clock error")
    if abs(ppm) < 20:
        print("  ✅ clocks are locked — drift is NOT the cause.")
    elif abs(ppm) < 200:
        print("  ⚠️  measurable drift. At this rate the resampler corrects roughly every")
        print(f"      {1000/abs(slope_ms_per_s)/1000:.0f}s — each correction a micro-glitch on transients,")
        print("      inaudible on a steady tone. This fits the symptom exactly.")
    else:
        print("  🔴 LARGE drift — this alone would explain audible smearing on music.")
else:
    print("  (too few confident probes to fit a slope)")

# ---------- amplitude comparison over the aligned region ----------
print(f"\n  ── level ──")
seg = min(len(ec), len(er) - g0)
if seg > 100:
    rr = sum(er[g0:g0+seg]) / seg
    cc = sum(ec[:seg]) / seg
    print(f"  mean envelope   reference {rr:8.0f}   captured {cc:8.0f}   ratio {cc/rr:.2f}×")
    if cc / rr > 1.6:
        print("  ⚠️  captured is much louder than source — the app's volume=2.0 gain.")
        print("      With music peaking near 66% FS this pushes past full scale.")
PY

echo
echo "════ files kept for further analysis ════"
echo "  original : $ORIGRAW"
echo "  captured : $CAPRAW"
