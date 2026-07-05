#!/bin/bash
F=/home/pi/flight.txt
while true; do
  echo "$(date +%H:%M:%S) up=$(cut -d. -f1 /proc/uptime) udc=$(cat /sys/class/udc/*/state 2>/dev/null) thr=$(vcgencmd get_throttled 2>/dev/null|cut -d= -f2) alt11=$(dmesg 2>/dev/null|grep -c "set_alt(1, 1)")" >> $F
  sync
  [ $(wc -l < $F) -gt 600 ] && tail -500 $F > $F.tmp && mv $F.tmp $F
  sleep 1
done
