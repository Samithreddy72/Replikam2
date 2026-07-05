#!/usr/bin/env bash
# macOS return-audio listener — GStreamer version with a real RTP jitter buffer.
# Plays the meeting/return audio the Pi sends back (client speaker -> you) on your default output.
#
# WHY GStreamer (2026-06-27): the Pi is WiFi-only, so return packets cross WiFi and arrive with
# timing jitter (bursts/stalls). The old ffplay listener had weak buffering and played that jitter
# straight through (audible stutter on shared YouTube/video). gst's rtpjitterbuffer holds ~250 ms
# and releases at a steady rate, absorbing the WiFi timing variance; opusdec use-inband-fec + the
# do-lost event conceal any actually-lost packets. Old ffplay version kept at .ffplay.bak.
#
# Usage:  ./mac-return-listen.sh [port]
#         RETURN_JITTER_MS=350 ./mac-return-listen.sh   # raise buffer if still jittery (more delay)
set -euo pipefail
PORT="${1:-5004}"
LATENCY="${RETURN_JITTER_MS:-180}"     # jitter-buffer depth (ms). Bigger = smoother but more delay.
                                        # Dropped 300->180 (2026-06-27) for LESS LATENCY — safe now
                                        # that Wi-Fi is -26dBm / 3ms direct. Raise back toward 300 if
                                        # the signal degrades and jitter returns.
GST="$(command -v gst-launch-1.0 || echo /opt/homebrew/bin/gst-launch-1.0)"
echo ">> Return audio on UDP ${PORT} -> default output | jitter buffer ${LATENCY}ms. Ctrl-C to stop."
# Plain decode (no FEC/PLC): tested cleaner — at a 300ms buffer the WiFi timing is absorbed with
# ZERO dropouts, and skipping packet-loss-concealment avoids the synthetic-audio artifacts PLC can
# add. audioresample quality=10 = SoX-grade, transparent to the Mac's output device rate.
# AUTO-RESTART loop (2026-06-27): the listener used to `exec` once — if gst died (Wi-Fi blip, audio
# device change, transient error) the backward audio went dead SILENTLY with no recovery. Now it
# self-restarts like the sender's video/mic loops, so a transient failure just blips for ~2s.
while true; do
  "$GST" -q \
    udpsrc port="$PORT" caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000" \
    ! rtpjitterbuffer latency="$LATENCY" \
    ! rtpopusdepay \
    ! opusdec \
    ! audioconvert ! audioresample quality=10 \
    ! audiodynamic mode=compressor characteristics=soft-knee ratio=0.1 threshold=0.12 \
    ! volume volume="${RETURN_GAIN:-2.0}" \
    ! audiodynamic mode=compressor characteristics=hard-knee ratio=0.08 threshold=0.97 \
    ! audioconvert \
    ! queue max-size-time=400000000 \
    ! osxaudiosink sync=false buffer-time=200000 latency-time=20000 || true
  echo ">> return listener ended, restarting in 2s..."
  sleep 2
done
