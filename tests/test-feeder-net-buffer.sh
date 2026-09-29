#!/bin/bash
# The video jitter buffer is capped at 100 ms whatever the profile says, and the feeder decodes in
# software. Runs the REAL script with a stub gst-launch and checks the argv it would execute.
#   bash tests/test-feeder-net-buffer.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "  PASS  $1"; }; no(){ fail=$((fail+1)); echo "  FAIL  $1"; }
mkdir "$T/bin"; printf '#!/bin/bash\nprintf "%%s " "$@" > %s/argv\n' "$T" > "$T/bin/python3"; chmod +x "$T/bin/python3"
run(){ # run <profile-file-contents or NONE>
  local cfg="$T/nofile"; [ "$1" != NONE ] && { printf '%s\n' "$1" > "$T/net"; cfg="$T/net"; }
  sed -e "s#^export PATH=#export PATH=$T/bin:#" -e "s#/etc/default/bridge-net#$cfg#g" -e "s#/usr/bin/python3#$T/bin/python3#g" \
    "$ROOT/pi/scripts/bridge-feeder-net.sh" > "$T/s.sh"; : > "$T/argv"; bash "$T/s.sh"
  awk '{print $2}' "$T/argv"; }
[ "$(run $'NET_VIDEO_LATENCY=300\nNET_AUDIO_LATENCY=300')" = 100 ] && ok "WAN profile (300) -> video buffer 100 ms" || no "WAN profile not capped"
[ "$(run $'NET_VIDEO_LATENCY=200\nNET_AUDIO_LATENCY=200')" = 100 ] && ok "LAN profile (200) -> video buffer 100 ms" || no "LAN profile not capped"
[ "$(run NONE)" = 100 ] && ok "no profile file -> 100 ms default" || no "default wrong"
[ "$(run 'NET_VIDEO_LATENCY=60')" = 60 ] && ok "a lower configured value is kept (60 ms)" || no "lower value not kept"
[ "$(run 'NET_VIDEO_LATENCY=junk')" = 100 ] && ok "non-numeric value -> 100 ms default" || no "non-numeric value not replaced by the default"
python3 -c 'import runpy; print(runpy.run_path("pi/scripts/bridge-video-receiver.py")["pipeline_description"](100))' > "$T/argv"
grep -q 'avdec_h264' "$T/argv" && ! grep -q 'v4l2h264dec' "$T/argv" && ok "software decoder (avdec_h264) only" || no "decoder is not avdec_h264"
grep -q 'v4l2sink device=/dev/video40 sync=false' "$T/argv" && ok "writes /dev/video40 with sync=false (unchanged)" || no "sink changed"
echo; echo "  $pass passed, $fail failed"; [ "$fail" -eq 0 ]
