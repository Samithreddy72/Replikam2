#!/bin/bash
# Flight recorder: one line per second -> /home/pi/flight.txt (ring, last 500).
#   pull=1  the client is actively pulling video frames right now (pump stat
#           lines in the last 3s — the dmesg set_alt probe never fired on the
#           6.12/v0.4.0 stack, so this replaces the always-0 alt11 field)
F=/home/pi/flight.txt
# Rotate through a temp file NEXT TO THE REAL FILE, not next to the symlink.
# /home/pi/flight.txt is redirected onto /data, so the append works — but "$F.tmp" resolves
# to /home/pi/flight.txt.tmp, and /home/pi is on the READ-ONLY root. The rotation therefore
# failed on EVERY pass: one "Read-only file system" line in the journal every second, and
# the ring never trimmed. This file reached 84,017 lines against an intended cap of 500,
# on the finite /data partition.
REAL="$(readlink -f "$F" 2>/dev/null || echo "$F")"
TMP="$(dirname "$REAL")/.flight.rotate.tmp"
while true; do
  PULL=$(journalctl -u bridge-uvcd --since "-3 seconds" -o cat 2>/dev/null | grep -c "pump: ok=")
  echo "$(date +%H:%M:%S) up=$(cut -d. -f1 /proc/uptime) udc=$(cat /sys/class/udc/*/state 2>/dev/null) thr=$(vcgencmd get_throttled 2>/dev/null|cut -d= -f2) pull=$([ "${PULL:-0}" -gt 0 ] && echo 1 || echo 0)" >> $F
  sync
  [ "$(wc -l < "$REAL" 2>/dev/null || echo 0)" -gt 600 ] \
    && tail -500 "$REAL" > "$TMP" 2>/dev/null && mv -f "$TMP" "$REAL" 2>/dev/null
  sleep 1
done
