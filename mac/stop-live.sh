#!/usr/bin/env bash
# RepliKam — ONE-COMMAND STOP for the Mac side. Cleanly ends a go-live session.
#   bash ~/Downloads/RepliKam/stop-live.sh
#
# Kills, in glitch-free order (wrappers BEFORE children so nothing respawns):
#   1. the go-live / mac-stream wrapper loops
#   2. the camera + mic ffmpeg senders
#   3. the return-audio listener (wrapper loop + gst)
#   4. caffeinate (lets the Mac sleep again)
# The Pi is NOT touched — its services idle harmlessly and are ready for the
# next go-live. The client's "UVC Camera" simply freezes/greys until you start again.

echo "== RepliKam STOP =="

# 1) wrapper loops first (they would otherwise respawn the ffmpegs)
pkill -f "go-live.sh"        2>/dev/null && echo "  ✓ go-live wrapper stopped"
pkill -f "mac-stream.sh"     2>/dev/null && echo "  ✓ sender wrapper stopped"
pkill -f "mac-return-listen" 2>/dev/null && echo "  ✓ listener wrapper stopped"
sleep 1

# 2) the actual media processes
pkill -f "ffmpeg.*rtp://.*:5000" 2>/dev/null && echo "  ✓ camera sender stopped"
pkill -f "ffmpeg.*rtp://.*:5002" 2>/dev/null && echo "  ✓ mic sender stopped"
pkill -f "gst-launch.*5004"      2>/dev/null && echo "  ✓ return-audio listener stopped"

# 3) sleep guard
pkill -f "caffeinate -dimsu" 2>/dev/null && echo "  ✓ caffeinate stopped (Mac may sleep again)"
sleep 1

# 4) verify nothing is left
LEFT=$(pgrep -f "mac-stream|go-live|ffmpeg.*rtp|gst-launch.*5004|caffeinate -dimsu" | wc -l | tr -d ' ')
if [ "$LEFT" = "0" ]; then
  echo "== ✅ ALL STOPPED — clean =="
else
  echo "== ⚠ $LEFT process(es) still running — force-killing =="
  pkill -9 -f "mac-stream|ffmpeg.*rtp|gst-launch.*5004" 2>/dev/null
  sleep 1
  echo "== done =="
fi
