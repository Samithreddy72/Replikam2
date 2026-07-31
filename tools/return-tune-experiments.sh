#!/bin/bash
# A/B the followed-rate click fixes REMOTELY — no card surgery per attempt.
#
# Context: at followed rates (44.1k/32k) the return audio has periodic clicks ("bars",
# 5-10ms spacing, ~66-77 discontinuities/sec); at 48k (resampler = pass-through) it is
# clean. Prime suspect: alsasrc's default clock-slaving (skew) making audioresample reset
# state. Each variant below is one theory; the winner becomes the default.
#
# Usage:  bash return-tune-experiments.sh <bridge-host> [variant]
#         bash return-tune-experiments.sh 192.168.1.13 list
#   Set the WINDOWS side to 44100 Hz first (the click-prone rate), keep music playing,
#   and make sure the presenter app is live so packets arrive on 127.0.0.1:5004.
set -uo pipefail
HOST="${1:?usage: $0 <bridge-host> [variant|list|clear|all]}"
V="${2:-all}"

# No associative arrays: macOS ships bash 3.2 (last GPLv2 release), where declare -A is
# a runtime error that bash -n cannot catch. Plain case dispatch runs everywhere.
variant_spec() {   # -> "props|pre"
  case "$1" in
    baseline)            echo "|" ;;
    slave-none)          echo "slave-method=none|" ;;
    no-clock)            echo "provide-clock=false|" ;;
    slave-none-no-clock) echo "slave-method=none provide-clock=false|" ;;
    audiorate)           echo "|audiorate" ;;
    combo)               echo "slave-method=none provide-clock=false|audiorate" ;;
    *) return 1 ;;
  esac
}
ORDER="baseline slave-none no-clock slave-none-no-clock audiorate combo"

[ "$V" = "list" ] && { for k in $ORDER; do echo "  $k: $(variant_spec $k)"; done; exit 0; }
[ "$V" = "clear" ] && { curl -s -m25 -X POST "http://$HOST:8080/api/return-tune" \
    -H 'content-type: application/json' -d '{"clear":true}'; echo; exit 0; }

GST="$(ls -d /var/folders/*/*/T/_MEI*/gst/gst-launch-1.0 2>/dev/null | head -1)"
[ -x "$GST" ] || GST="$(command -v gst-launch-1.0)"
export GST_PLUGIN_PATH="$(dirname "$GST")/plugins" DYLD_LIBRARY_PATH="$(dirname "$GST")"
CAPS='application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000'

apply() { # apply <props> <pre>
  curl -s -m25 -X POST "http://$HOST:8080/api/return-tune" \
    -H 'content-type: application/json' \
    -d "{\"props\":\"$1\",\"pre\":\"$2\"}" 2>/dev/null
}

measure() { # 20s capture -> "disc/s zero/s secs"
  pkill -f 'gst-launch.*5004' 2>/dev/null; sleep 1; rm -f /tmp/tune.raw
  "$GST" -q udpsrc port=5004 caps="$CAPS" ! rtpjitterbuffer latency=250 ! rtpopusdepay \
    ! opusdec ! audioconvert ! audio/x-raw,format=S16LE,channels=2,rate=48000 \
    ! filesink location=/tmp/tune.raw >/dev/null 2>&1 &
  sleep 21; pkill -f 'location=/tmp/tune.raw' 2>/dev/null; sleep 1
  python3 - <<'PY'
import struct, statistics as st
try: raw=open("/tmp/tune.raw","rb").read()
except: print("0 0 0"); raise SystemExit
n=len(raw)//4
if n<48000: print("0 0 0"); raise SystemExit
s=struct.unpack("<%dh"%(n*2), raw[:n*4]); L=s[::2]; sr=48000; dur=n/sr
z=0;cur=0
for x in L:
    if x==0: cur+=1
    else:
        if cur>=48: z+=1
        cur=0
d=[L[i]-L[i-1] for i in range(1,len(L))]
sd=st.pstdev(d[:200000]) or 1
sp=sum(1 for x in d if abs(x)>10*sd)
print("%.1f %.2f %.1f"%(sp/dur, z/dur, dur))
PY
  rm -f /tmp/tune.raw
}

run_variant() {
  local name="$1" spec; spec="$(variant_spec "$1")" || { echo "unknown variant $1"; return 1; }
  local props="${spec%%|*}" pre="${spec##*|}"
  printf "── %-20s props='%s' pre='%s'\n" "$name" "$props" "$pre"
  local r; r=$(apply "$props" "$pre")
  echo "$r" | grep -q '"ok": *true' || { echo "   ⚠️ apply failed: $r"; return; }
  sleep 6                                     # service restart + pipeline settle
  read -r disc zero dur <<< "$(measure)"
  printf "   -> %s disc/s   %s zero/s   (%ss captured)\n" "$disc" "$zero" "$dur"
  echo "$name $disc $zero" >> /tmp/tune-results.txt
}

: > /tmp/tune-results.txt
if [ "$V" = "all" ]; then
  echo "reference: 48k clean ≈ 10 disc/s (music-level) · bars ≈ 66-77 disc/s"
  for k in $ORDER; do run_variant "$k"; done
  echo
  echo "════ SCOREBOARD (lower = better) ════"
  sort -k2 -n /tmp/tune-results.txt | awk '{printf "  %-22s %6s disc/s  %5s zero/s\n",$1,$2,$3}'
  echo
  echo "NOTE: tuning left at the LAST variant — apply the winner with:"
  echo "  bash $0 $HOST <winner>     (or 'clear' for defaults)"
else
  run_variant "$V"
fi
# restore the app player
curl -s -m4 -X POST http://127.0.0.1:8765/api/return -d '{"on":false}' >/dev/null 2>&1; sleep 1
curl -s -m4 -X POST http://127.0.0.1:8765/api/return -d '{"on":true}' >/dev/null 2>&1
