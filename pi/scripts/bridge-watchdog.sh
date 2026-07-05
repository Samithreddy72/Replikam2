#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
L(){ logger -t bridge-watchdog "$*"; }
lsmod | grep -q '^libcomposite' || modprobe libcomposite
[ -e /dev/video40 ] || { L "video40 missing -> modprobe v4l2loopback"; modprobe v4l2loopback video_nr=40 card_label=BridgeCam exclusive_caps=0; }
G=/sys/kernel/config/usb_gadget/g1
if [ ! -d "$G/functions/uvc.0" ] || [ ! -d "$G/functions/uac2.usb0" ] || [ ! -d /proc/asound/UAC2Gadget ]; then
  L "gadget/UAC2 missing -> restart bridge-gadget"; systemctl restart bridge-gadget
fi
for s in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio; do
  st=$(systemctl is-active "$s")
  case "$st" in active|activating|reloading) : ;; *) L "$s is $st -> restart"; systemctl restart "$s" ;; esac
done
