#!/bin/bash
# Observation only. systemd owns bounded per-service recovery. A second restart
# loop defeated its rate limits and could re-enumerate a connected room laptop.
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
for s in bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio; do
  st=$(systemctl is-active "$s")
  case "$st" in active|activating|reloading) : ;;
    *) logger -t bridge-watchdog "$s is $st; inspect service and power diagnostics; no automatic gadget reset" ;;
  esac
done
