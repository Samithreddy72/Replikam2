#!/bin/bash
# Clear a wedged macOS camera and prove it works again.
#
# Symptom this fixes: the camera opens but delivers no frames — ffmpeg either hangs or
# returns "Input/output error", the green LED never lights, and NetBridge reports
# "Your video arriving at bridge" red while voice and return audio are fine.
#
# Usual cause: a process was killed while it held the camera, leaving the capture
# daemons in a state where they hand out the device but never start the stream.
#
# These daemons are launchd-managed and relaunch automatically. Killing them is safe.

set -u
echo
echo "NetBridge — camera repair"
echo "========================="
echo

echo "1. What is running now"
for d in appleh16camerad cameracaptured avconferenced \
         com.apple.cmio.registerassistantservice \
         com.apple.cmio.videodriverkithostextension; do
  pid=$(pgrep -x "$d" 2>/dev/null | head -1)
  if [ -n "$pid" ]; then echo "     $d  (pid $pid)"; else echo "     $d  — not running"; fi
done
echo

echo "2. Anything holding the camera right now"
holders=$(lsof 2>/dev/null | grep -iE "AppleH1[0-9]CamIn|/dev/video" | awk '{print $1" (pid "$2")"}' | sort -u)
if [ -n "$holders" ]; then
  echo "$holders" | sed 's/^/     /'
  echo "     ^ close these first, then run this script again"
else
  echo "     nothing — the camera is free"
fi
echo

echo "3. Restarting the capture daemons (needs your password)"
sudo killall appleh16camerad cameracaptured 2>/dev/null \
  && echo "     killed — launchd is restarting them" \
  || echo "     nothing matched by that name; trying the CoreMediaIO assistants"
sudo killall com.apple.cmio.registerassistantservice 2>/dev/null && echo "     restarted the CoreMediaIO assistant"
echo "     waiting 5s for them to come back"
sleep 5
echo

echo "4. Testing the camera"
FF=$(ls -d /var/folders/*/*/T/_MEI*/ffmpeg 2>/dev/null | head -1)
[ -z "$FF" ] && FF=$(command -v ffmpeg)
if [ -z "$FF" ]; then
  echo "     no ffmpeg available to test with — open Photo Booth instead."
  echo "     If Photo Booth shows your face, the camera is fixed."
  exit 0
fi

out=$("$FF" -hide_banner -nostdin -f avfoundation -framerate 30 -video_size 1280x720 \
      -pixel_format uyvy422 -i "0:none" -frames:v 30 -f null - 2>&1)
if echo "$out" | grep -q "frame="; then
  echo "     ✅ CAMERA WORKS — captured frames successfully"
  echo
  echo "     Now in NetBridge:  End session  ->  Go live"
else
  echo "     ❌ camera still not delivering frames"
  echo "     ---- what ffmpeg said ----"
  echo "$out" | tail -6 | sed 's/^/     /'
  echo
  echo "     Restart the Mac. That always clears this."
fi
echo
