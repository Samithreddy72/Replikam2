#!/bin/bash
# One-shot bring-up of the whole UVC test chain (run after a reboot)
set +e
sudo modprobe libcomposite
sudo modprobe -r v4l2loopback 2>/dev/null
sudo modprobe v4l2loopback video_nr=40 card_label=BridgeCam exclusive_caps=1
sudo v4l2loopback-ctl set-caps /dev/video40 "YUYV:424x240@30/1"
sudo /usr/local/bin/bridge-gadget-down.sh
sudo bash /home/pi/uvc-raw-setup.sh start >/dev/null 2>&1
pkill -9 -f gst-launch 2>/dev/null; sleep 1
setsid bash /home/pi/feed-test.sh </dev/null >/dev/null 2>&1 &
sleep 4
pkill -9 -f "uvc-gadget -d" 2>/dev/null; sleep 1
setsid bash /home/pi/uvcd.sh </dev/null >/dev/null 2>&1 &
sleep 3
echo "UDC=$(cat /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null)"
echo "feeder=$(pgrep -af gst-launch >/dev/null && echo UP || echo DOWN)"
echo "uvcd=$(pgrep -af 'uvc-gadget -d' >/dev/null && echo UP || echo DOWN)"
echo "loopback=$(v4l2-ctl -d /dev/video40 --get-fmt-video 2>/dev/null | grep -o "'[A-Z0-9]*'" | head -1)"
