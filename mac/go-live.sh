#!/usr/bin/env bash
# RepliKam — ONE-COMMAND go-live for the Mac side.
# RUN THIS FROM YOUR OWN TERMINAL (or the double-click launcher) — the camera + mic need the
# Terminal's macOS TCC permission; a background/agent process gets a hung camera + silent mic.
#
#   bash ~/Downloads/RepliKam/go-live.sh
#
# Brings up, in order: Tailscale -> Pi reachability+health gate -> caffeinate (no-sleep) ->
# return-audio listener (backward, auto-restarting) -> camera+mic sender (forward, foreground).
# Ctrl-C cleanly ends the whole session (caffeinate + sender stop; listener keeps the port freed).
set -uo pipefail

REPO="$HOME/Downloads/RepliKam"
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
PI="${PI:-100.91.108.50}"           # Pi Tailscale IP (stable on every network)
export PATH="/opt/homebrew/bin:$PATH"

bar(){ printf '\n=== %s ===\n' "$1"; }

bar "1/6  Tailscale"
if "$TS" status >/dev/null 2>&1; then echo "  ✅ up"; else echo "  starting..."; "$TS" up && echo "  ✅ up"; fi

bar "2/6  Pi reachability + health"
if ! ping -c1 -t3 "$PI" >/dev/null 2>&1; then
  echo "  ❌ Pi UNREACHABLE at $PI"
  echo "     -> power the Pi on and give it Wi-Fi (it auto-joins 'Raja Shekar_5G'), wait ~40s, re-run."
  exit 1
fi
SVC=$(ssh -i ~/.ssh/pi_bridge -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new pi@"$PI" \
  'systemctl is-active bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio | grep -c active' 2>/dev/null || echo 0)
UDC=$(ssh -i ~/.ssh/pi_bridge -o ConnectTimeout=8 pi@"$PI" 'cat /sys/class/udc/*/state' 2>/dev/null || echo unknown)
echo "  Pi services: ${SVC}/5 | USB client: ${UDC}"
if [ "${SVC}" != "5" ]; then
  echo "  ⚠ not all services up — attempting a media restart..."
  ssh -i ~/.ssh/pi_bridge -o ConnectTimeout=8 pi@"$PI" 'bridge restart' >/dev/null 2>&1 || true
  sleep 4
  SVC=$(ssh -i ~/.ssh/pi_bridge -o ConnectTimeout=8 pi@"$PI" 'systemctl is-active bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio | grep -c active' 2>/dev/null || echo 0)
  echo "  after restart: ${SVC}/5"
fi
[ "${UDC}" = "configured" ] || echo "  ⚠ no laptop detected on the Pi USB-C (USB client='${UDC}') — plug the client laptop into the Pi."

bar "3/6  Keep this Mac awake"
caffeinate -dimsu -w $$ &
echo "  ✅ caffeinate active until you Ctrl-C (no idle-sleep can drop the call)"

bar "4/6  Audio routing -> headphones (echo-free)"
# osxaudiosink (the return listener below) binds the default OUTPUT at startup — so set the
# headphones as default FIRST, else return audio plays on the laptop speakers and you get echo.
# Override the name with HEADPHONES="..." if you use a different headset.
HP="${HEADPHONES:-Bassheads 100 C}"
if command -v SwitchAudioSource >/dev/null 2>&1 && SwitchAudioSource -a -t output 2>/dev/null | grep -qxF "$HP"; then
  SwitchAudioSource -s "$HP" -t output >/dev/null 2>&1
  SwitchAudioSource -s "$HP" -t input  >/dev/null 2>&1
  # pin the forward mic to the headset's avfoundation index (so your voice = headset mic, no laptop mic)
  HP_IDX=$(ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | sed -n '/audio devices/,/^$/p' | grep -F "$HP" | grep -oE '\[[0-9]+\]' | tr -d '[]' | head -1)
  [ -n "$HP_IDX" ] && export AUDIO_DEV="${AUDIO_DEV:-$HP_IDX}"
  echo "  ✅ in+out -> $HP (echo-free) | forward mic AUDIO_DEV=${AUDIO_DEV:-1}"
else
  echo "  ⚠ '$HP' not connected — output stays '$(SwitchAudioSource -c -t output 2>/dev/null)'; plug headphones in to avoid echo"
fi

bar "5/6  Backward audio (return listener)"
if pgrep -f "gst-launch.*5004" >/dev/null; then
  echo "  ✅ already running"
else
  NO_RETURN_REGISTER=1 nohup "$REPO/mac-return-listen.sh" >/tmp/replikam-return.log 2>&1 &
  sleep 2
  pgrep -f "gst-launch.*5004" >/dev/null && echo "  ✅ started (auto-restarting)" || { echo "  ❌ listener failed — is GStreamer installed? 'brew install gstreamer'"; }
fi

bar "6/6  Forward camera + mic (foreground)"
echo "  Starting sender to $PI. Click ALLOW if macOS prompts for camera/mic."
echo "  >>> Ctrl-C here ends the whole session. <<<"
sleep 1
exec "$REPO/mac-stream.sh"
