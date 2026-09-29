#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
# WAN/LAN jitterbuffer tuning (bridge profile <lan|wan>); default 100ms if unset.
[ -f /etc/default/bridge-net ] && . /etc/default/bridge-net
# Video buffer cap (2026-09-22, owner's decision): the profile stays WAN (300 ms), but the VIDEO
# jitter buffer is capped at 100 ms - the original proven default. 300 ms added ~200 ms of lag to
# every frame on a direct path. Voice keeps its own profile value (bridge-feeder-audio.sh).
VLAT=${NET_VIDEO_LATENCY:-100}
case "$VLAT" in ''|*[!0-9]*) VLAT=100 ;; esac      # not a plain number: use the default
[ "$VLAT" -gt 100 ] && VLAT=100
# The receiver publishes one atomic latest RAM frame for the STATIC UVC source.
# It does not write to a V4L2 loopback device; the USB pump owns output pacing.
# Software decoder (avdec_h264), deliberately. The Pi's hardware decoder (v4l2h264dec) was tried
# on 2026-09-22 and measured WORSE live: 74% of a core against 51%, because converting frames out
# of the decoder's buffers into YUY2 cost ~70% of one core on a single thread.
exec /usr/bin/python3 /usr/local/bin/bridge-video-receiver.py "$VLAT"
