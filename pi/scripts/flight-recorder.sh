#!/bin/bash
# Flight recorder: one line per second -> /home/pi/flight.txt (ring, last 500).
#   pull=1  the client is actively pulling video frames right now (pump stat
#           lines in the last 3s — the dmesg set_alt probe never fired on the
#           6.12/v0.4.0 stack, so this replaces the always-0 alt11 field)
F=/home/pi/flight.txt
while true; do
  PULL=$(journalctl -u bridge-uvcd --since "-3 seconds" -o cat 2>/dev/null | grep -c "pump: ok=")
  echo "$(date +%H:%M:%S) up=$(cut -d. -f1 /proc/uptime) udc=$(cat /sys/class/udc/*/state 2>/dev/null) thr=$(vcgencmd get_throttled 2>/dev/null|cut -d= -f2) pull=$([ "${PULL:-0}" -gt 0 ] && echo 1 || echo 0)" >> $F
  sync
  [ $(wc -l < $F) -gt 600 ] && tail -500 $F > $F.tmp && mv $F.tmp $F
  sleep 1
done
