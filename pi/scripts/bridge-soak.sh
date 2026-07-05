#!/bin/bash
S=""
for s in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio; do
  a=$(systemctl is-active "$s"); S="$S$(echo "$a"|cut -c1)"
done
echo "$(date '+%m-%d %H:%M:%S') up=$(cut -d. -f1 /proc/uptime)s svc=$S udc=$(cat /sys/class/udc/*/state 2>/dev/null) thr=$(vcgencmd get_throttled 2>/dev/null|cut -d= -f2) temp=$(vcgencmd measure_temp 2>/dev/null|cut -d= -f2) feedR=$(systemctl show -p NRestarts --value bridge-feeder-net) uvcR=$(systemctl show -p NRestarts --value bridge-uvcd)" >> /home/pi/soak.log
