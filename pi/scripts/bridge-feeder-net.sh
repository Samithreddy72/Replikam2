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
# sync=false: write decoded frames to /dev/video40 as they arrive (paced by the rtpjitterbuffer's
# RTP-timestamp release). sync=true was tried but with a live RTP source the PTS don't align to
# pipeline running-time, so v4l2sink rendered ~nothing (frozen). The real judder fix is in the
# uvc-gadget pump (re-send the cached latest frame on EAGAIN), not here.
# Software decoder (avdec_h264), deliberately. The Pi's hardware decoder (v4l2h264dec) was tried
# on 2026-09-22 and measured WORSE live: 74% of a core against 51%, because converting frames out
# of the decoder's buffers into YUY2 cost ~70% of one core on a single thread.
exec gst-launch-1.0 udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" ! rtpjitterbuffer latency=$VLAT ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! videoscale ! videorate ! video/x-raw,format=YUY2,width=480,height=270,framerate=20/1 ! v4l2sink device=/dev/video40 sync=false
