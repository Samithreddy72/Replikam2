#!/usr/bin/env bash
# Did the session actually END, and did it leave anything behind?
#
# WHY THIS EXISTS
# ---------------
# "I ended the session" has meant three different things on this project, and none of them
# was reliably true:
#
#   2026-08-24  /api/stop returned ok, the supervisor restarted the legs seconds later, and
#               the camera light stayed on. There was no way to end a session short of
#               quitting the app.
#   2026-08-24  the app was killed mid-capture; ffmpeg never released the camera and every
#               later launch got a device that opened and delivered no frames.
#   earlier     a closed browser tab looked like the end of a session while everything
#               carried on streaming.
#
# A green camera light on a machine whose owner believes they have finished is not a cosmetic
# bug, so this checks the things that would actually still be running - and checks the ROOM's
# side too, because the bridge keeps its own view of whether media is flowing.
#
#   bash tools/session-end-check.sh [bridge-ip]
set -uo pipefail
BRIDGE="${1:-192.168.1.11}"
APP="http://127.0.0.1:8765"

PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
no()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
warn() { WARN=$((WARN+1)); printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
sec()  { printf '\n\033[1m── %s\033[0m\n' "$1"; }

jget() { python3 -c "
import json,sys,re
try: d=json.loads(re.sub(r'[\x00-\x1f]',' ',sys.stdin.read()))
except Exception: print(''); raise SystemExit
v=d
for k in '$1'.split('.'):
    v = (v or {}).get(k) if isinstance(v,dict) else None
print(json.dumps(v) if not isinstance(v,str) else v)"; }

echo "Session end check"
echo "================="

sec "Nothing is still capturing on this Mac"
ENC=$(ps -Ao args | grep -c '[h]264_videotoolbox')
[ "$ENC" = "0" ] && ok "no video encoder running" || no "$ENC video encoder(s) STILL RUNNING"
# The mic leg is a separate ffmpeg; it holds the microphone, which is just as private.
MIC=$(ps -Ao args | grep -c '[f]fmpeg .*avfoundation')
[ "$MIC" = "0" ] && ok "no capture ffmpeg holding camera or microphone" \
                 || no "$MIC capture process(es) still holding a device"
# The camera is held by an ffmpeg reading avfoundation with a VIDEO device index ("-i 0:none"),
# not by any file lsof can see. The first version of this check grepped lsof for AppleH1xCamIn
# and PASSED while an encoder was actively capturing - a check that cannot fail, which is worse
# than no check because it reads as reassurance.
CAM=$(ps -Ao args | grep '[f]fmpeg' | grep -c -- '-i [0-9]:')
[ "$CAM" = "0" ] && ok "camera released — no process reading a video device" \
                 || no "$CAM process(es) still capturing VIDEO — the green light is ON"
AUD=$(ps -Ao args | grep '[f]fmpeg' | grep -c -- '-i :[0-9]')
[ "$AUD" = "0" ] && ok "microphone released" || no "$AUD process(es) still capturing AUDIO"
GST=$(ps -Ao args | grep -c '[g]st-launch-1.0')
[ "$GST" = "0" ] && ok "no GStreamer player left running" || no "$GST player(s) still running"

sec "The media sockets are free"
# WHO holds the port matters more than whether it is held. The mesh helper keeps the RTP
# ports bound between sessions so the next go-live is instant, and it captures nothing - no
# camera, no microphone, no audio played out. Failing on that would flag a healthy idle app
# and teach people to ignore this check. A CAPTURE or PLAYER process holding one is the real
# leak, and that is what gets flagged.
for p in 5000 5002 5004; do
  HOLDERS=$(lsof -nP -iUDP 2>/dev/null | grep ":$p" | awk '{print $1}' | sort -u | tr '\n' ' ')
  BAD=$(printf '%s' "$HOLDERS" | tr ' ' '\n' | grep -cE 'ffmpeg|gst-launch')
  if [ -z "${HOLDERS// /}" ]; then ok "UDP $p released"
  elif [ "$BAD" = "0" ]; then ok "UDP $p held only by the mesh helper (${HOLDERS%% }) — idle, captures nothing"
  else no "UDP $p still held by a capture/player process: $HOLDERS"
  fi
done

sec "The app agrees the session is over"
S=$(curl -s --max-time 5 "$APP/api/state" 2>/dev/null || true)
if [ -z "$S" ]; then
  ok "app is not running (nothing to disagree with)"
else
  L=$(printf '%s' "$S" | jget live)
  [ "$L" = "false" ] && ok "app reports live=false" \
    || no "app still reports live=$L — the session did not end, whatever the UI said"
  # The bug that made this file necessary: a supervisor undoing a deliberate stop.
  R=$(printf '%s' "$S" | jget guard.repairs)
  [ "$R" = "{}" ] || [ -z "$R" ] && ok "supervisor is not repairing anything" \
    || warn "supervisor shows repairs after a stop: $R"
fi

sec "The camera daemons were not wedged on the way out"
# A SIGKILLed ffmpeg leaves these handing out the device while delivering no frames. If they
# have just restarted, something died holding the camera.
for d in cameracaptured appleh16camerad; do
  E=$(ps -Ao etime,comm | awk -v d="$d" '$2 ~ d {print $1; exit}')
  if [ -z "$E" ]; then warn "$d not running"
  else
    SEC=$(printf '%s' "$E" | awk -F: '{n=NF; s=0; m=1; for(i=n;i>0;i--){s+=$i*m; m*= (i==n?60:60)} print s}')
    if [ "${SEC:-0}" -gt 120 ]; then ok "$d up ${E} — never restarted, so nothing wedged it"
    else warn "$d restarted ${E} ago — something may have died holding the camera"; fi
  fi
done

sec "The room's side stopped too"
B=$(curl -s --max-time 6 "http://$BRIDGE:8080/api/status" 2>/dev/null || true)
if [ -z "$B" ]; then
  warn "bridge $BRIDGE not reachable — cannot confirm its view"
else
  ST=$(printf '%s' "$B" | jget streams)
  # Only the two FORWARD streams belong to the presenter's session. `return` is the bridge
  # capturing what the meeting laptop plays into it over USB, and that continues perfectly
  # correctly after the presenter has gone home - flagging it would train people to ignore
  # this check.
  case "$ST" in
    *'"video": true'*) no "bridge still receiving VIDEO from this Mac: $ST" ;;
    *'"voice": true'*) no "bridge still receiving VOICE from this Mac: $ST" ;;
    *) ok "bridge is no longer receiving anything from this Mac" ;;
  esac
  case "$ST" in
    *'"return": true'*) printf '  \033[2m       (return still true: the meeting laptop is playing into the bridge — not ours to stop)\033[0m\n' ;;
  esac
  # udc stays "configured" while the USB cable is in — that is correct and not a leak.
  printf '  \033[2m       (usb still attached: %s — expected while the cable is in)\033[0m\n' \
    "$(printf '%s' "$B" | jget udc)"
fi

echo
printf '\033[1m  %d passed, %d failed, %d warnings\033[0m\n' "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -eq 0 ] && echo "  → the session ended cleanly" || echo "  → SOMETHING IS STILL RUNNING"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
