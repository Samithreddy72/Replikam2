#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin
# WAN/LAN jitterbuffer tuning (bridge profile <lan|wan>); default 100ms if unset.
[ -f /etc/default/bridge-net ] && . /etc/default/bridge-net
# sync=false: write decoded frames to /dev/video40 as they arrive (paced by the rtpjitterbuffer's
# RTP-timestamp release). sync=true was tried but with a live RTP source the PTS don't align to
# pipeline running-time, so v4l2sink rendered ~nothing (frozen). The real judder fix is in the
# uvc-gadget pump (re-send the cached latest frame on EAGAIN), not here.
exec gst-launch-1.0 udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" ! rtpjitterbuffer latency=${NET_VIDEO_LATENCY:-100} ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! videoscale ! videorate ! video/x-raw,format=YUY2,width=320,height=180,framerate=20/1 ! v4l2sink device=/dev/video41 sync=false
