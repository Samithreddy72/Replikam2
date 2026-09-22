#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
# WAN/LAN jitterbuffer tuning (bridge profile <lan|wan>); default 100ms if unset.
[ -f /etc/default/bridge-net ] && . /etc/default/bridge-net
# sync=false: write decoded frames to /dev/video40 as they arrive (paced by the rtpjitterbuffer's
# RTP-timestamp release). sync=true was tried but with a live RTP source the PTS don't align to
# pipeline running-time, so v4l2sink rendered ~nothing (frozen). The real judder fix is in the
# uvc-gadget pump (re-send the cached latest frame on EAGAIN), not here.
# Decode on the Pi 4's H.264 hardware block (bcm2835-codec, exposed as v4l2h264dec on
# /dev/video10) instead of the CPU. At 640x360 the software decoder (avdec_h264) held a
# 900 MHz-capped Pi at load ~10 on 4 cores (2026-09-22): choppy video, growing lag, more
# current drawn, deeper brownouts. Falls back to avdec_h264 when the element or the device
# node is missing; if the hardware path fails at runtime the service crash-loops and
# bridge-run.sh's auto-rollback restores the baked-in (software) script.
DEC=avdec_h264
if [ -e /dev/video10 ] && gst-inspect-1.0 v4l2h264dec >/dev/null 2>&1; then
  DEC=v4l2h264dec
fi
echo "feeder-net: video decoder = $DEC" >&2
# alignment=au: v4l2h264dec wants whole access units; h264parse converts, avdec accepts it too.
exec gst-launch-1.0 udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" ! rtpjitterbuffer latency=${NET_VIDEO_LATENCY:-100} ! rtph264depay ! h264parse ! video/x-h264,stream-format=byte-stream,alignment=au ! $DEC ! videoconvert ! videoscale ! videorate ! video/x-raw,format=YUY2,width=640,height=360,framerate=20/1 ! v4l2sink device=/dev/video40 sync=false
