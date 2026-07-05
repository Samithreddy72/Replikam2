#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
set +e
modprobe libcomposite
modprobe v4l2loopback video_nr=40 card_label=BridgeCam exclusive_caps=1 max_buffers=16
[ -e /dev/video41 ] || v4l2loopback-ctl add -x 1 -n BridgeCamX -b 16 41
v4l2loopback-ctl set-caps /dev/video41 "YUYV:320x180@20/1" 2>/dev/null || true
V=$(command -v v4l2loopback-ctl); "$V" set-caps /dev/video40 "YUYV:320x180@20/1"
/usr/local/bin/bridge-gadget-down.sh
bash /home/pi/uvc-raw-setup.sh start
